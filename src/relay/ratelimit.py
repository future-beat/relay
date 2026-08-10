"""Phase 1 security perimeter: burst limiting and the daily spend ceiling.

Two controls with deliberately different lifetimes. The moving window bounds
burst — it lives in process memory and is expected to vanish on a cold start,
because a scale-to-zero machine that forgets who was hammering it an hour ago is
fine. The daily ceiling bounds aggregate Claude spend and is derived from the
`runs` table, so it survives those same cold starts, which is the whole point:
in-memory buckets cannot enforce a dollar budget across restarts.

Both are exposed as plain callables for FastAPI route dependencies, never
middleware — see auth.py for why streaming responses make middleware unusable
for rejections.
"""

import ipaddress
import itertools
import logging
import math
import time
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from limits import RateLimitItem, parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter

from .config import settings
from .db import Database

logger = logging.getLogger("relay.ratelimit")

# MemoryStorage constructs safely outside a running event loop, so these can be
# module-level singletons shared by every request.
_storage = MemoryStorage()
_limiter = MovingWindowRateLimiter(_storage)

# Cost of runs admitted but not yet written to `runs`: token -> (expires_at, usd).
#
# Self-expiring because the release is not guaranteed to happen. Starlette can
# cancel a streaming response before its generator is ever started, and a `finally`
# in an async generator that never began does not run — so that run's claim is
# never handed back. Without a TTL, ceil(max_daily / max_run) such requests pin the
# ceiling shut for the life of the process, which is a remotely triggerable outage
# rather than a strict failure. Identity-bearing for the same reason: a release must
# free exactly the claim it was given, never another run's and never whatever
# max_run_cost_usd happens to read at release time.
RESERVATION_TTL_S = 300.0
_reservations: dict[int, tuple[float, float]] = {}
_tokens = itertools.count()

# (bucket, tier) -> the settings attribute holding that limit string. The item
# itself is parsed on demand, never at import: parsing here would freeze the
# value before a test could monkeypatch it.
_LIMIT_SETTINGS: dict[tuple[str, str], str] = {
    # Consumed before a tier is known, which is what meters the failed-auth path.
    ("auth", "anon"): "anon_auth_limit",
    ("process", "demo"): "demo_process_limit",
    ("process", "owner"): "owner_process_limit",
    ("create", "demo"): "demo_create_limit",
    ("create", "owner"): "owner_create_limit",
    ("read", "demo"): "demo_read_limit",
    ("read", "owner"): "owner_read_limit",
}

_items: dict[tuple[str, str], RateLimitItem] = {}

DAILY_SPEND_SQL = (
    "SELECT COALESCE(SUM(cost_usd), 0.0) FROM runs"
    " WHERE created_at >= datetime('now', 'start of day')"
)


def _limit_item(bucket: str, tier: str) -> RateLimitItem:
    key = (bucket, tier)
    if key not in _items:
        _items[key] = parse(getattr(settings, _LIMIT_SETTINGS[key]))
    return _items[key]


def client_ip(request: Request) -> str:
    """Resolve the caller's IP, trusting the proxy header only where a proxy sets it.

    Unconditional trust is a rate-limit bypass: off the Fly proxy the header is
    fully client-controlled, so an attacker mints a fresh bucket per request.
    """
    if settings.trust_proxy_header:
        # Last value wins: only the nearest proxy's append is authoritative, and a
        # client-supplied duplicate can only ever arrive ahead of it.
        values = request.headers.getlist("fly-client-ip")
        if values:
            candidate = values[-1].strip()
            try:
                # Parsing normalises the value and rejects anything that is not an
                # address — an unvalidated string becomes its own bucket, and a
                # "/" in it would inject extra segments into the limiter's key.
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                logger.warning(
                    "ratelimit.bad_proxy_header",
                    extra={"ctx": {"value": candidate[:64]}},
                )
    return request.client.host if request.client else "unknown"


async def enforce(bucket: str, tier: str, request: Request) -> None:
    """Consume one unit of the (bucket, tier, ip) window, or raise 429."""
    item = _limit_item(bucket, tier)
    ip = client_ip(request)
    if await _limiter.hit(item, bucket, tier, ip):
        return

    stats = await _limiter.get_window_stats(item, bucket, tier, ip)
    retry_after = max(1, math.ceil(stats.reset_time - time.time()))
    logger.info(
        "ratelimit.exceeded",
        extra={"ctx": {"bucket": bucket, "tier": tier, "ip": ip, "retry_after": retry_after}},
    )
    # A dict detail diverges from the short-string HTTPException convention used
    # elsewhere; D-08 requires the rejection body to read as product copy.
    raise HTTPException(
        429,
        detail={
            "error": "rate_limited",
            "limit": str(item),
            "tier": tier,
            "retry_after_seconds": retry_after,
            "note": (
                f"The {tier} key is capped at {item} on this endpoint."
                f" Try again in {retry_after}s — this is a deliberate cost control"
                " on the public demo, not an outage."
            ),
        },
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(item.amount),
            "X-RateLimit-Remaining": str(stats.remaining),
            "X-RateLimit-Reset": str(int(stats.reset_time)),
        },
    )


def next_utc_midnight(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def spent_today(conn: Database, *, now: float | None = None) -> float:
    """Today's Claude spend in USD: rows written since UTC midnight, plus what
    currently-streaming runs could still cost.

    `datetime('now')` (which writes `created_at`) and `datetime('now', 'start of
    day')` are both UTC, so the comparison needs no timezone conversion. `now` is
    a monotonic reading forwarded to reserved_usd().
    """
    return float(conn.execute(DAILY_SPEND_SQL).fetchone()[0]) + reserved_usd(now)


def _prune(now: float) -> None:
    for token, (expires_at, _) in list(_reservations.items()):
        if expires_at <= now:
            _reservations.pop(token, None)


def reserved_usd(now: float | None = None) -> float:
    """Worst-case cost of runs admitted but not yet written to `runs`.

    Expired claims are dropped on read, which is what bounds a lost release to
    RESERVATION_TTL_S instead of the process's lifetime. `now` is a monotonic
    reading, injectable so expiry is testable without waiting out the TTL.
    """
    now = time.monotonic() if now is None else now
    _prune(now)
    return sum(usd for _, usd in _reservations.values())


def reserve_run() -> int:
    """Claim this run's worst-case cost before it starts, returning its token.

    `record_run` only fires once the SSE generator finishes, so without this a
    burst of concurrent requests all read the same stale SUM and all pass the
    ceiling — overshooting by up to concurrency x max_run_cost_usd.
    """
    now = time.monotonic()
    _prune(now)
    token = next(_tokens)
    _reservations[token] = (now + RESERVATION_TTL_S, settings.max_run_cost_usd)
    return token


def release_run(token: int | None) -> None:
    """Hand back one claim. Idempotent, and never frees another run's."""
    if token is not None:
        _reservations.pop(token, None)


def enforce_daily_budget(conn: Database) -> None:
    """Raise 503 once today's spend reaches the configured ceiling."""
    spent = spent_today(conn)
    if spent < settings.max_daily_cost_usd:
        return

    resets_at = next_utc_midnight()
    retry_after = max(1, math.ceil((resets_at - datetime.now(UTC)).total_seconds()))
    logger.info(
        "budget.daily_exceeded",
        extra={"ctx": {"spent_usd": round(spent, 4), "limit_usd": settings.max_daily_cost_usd}},
    )
    raise HTTPException(
        503,
        detail={
            "error": "daily_budget_exhausted",
            "spent_usd": round(spent, 4),
            "limit_usd": settings.max_daily_cost_usd,
            "resets_at": resets_at.isoformat(),
            "note": (
                "This demo caps its Claude spend at"
                f" ${settings.max_daily_cost_usd:.2f}/day and resets at 00:00 UTC."
                " Come back after the reset — the cap is a feature, not a fault."
            ),
        },
        headers={"Retry-After": str(retry_after)},
    )


async def reset_limits() -> None:
    """Test hook — MemoryStorage and the reservations are process-wide state."""
    await _storage.reset()
    _items.clear()
    _reservations.clear()
