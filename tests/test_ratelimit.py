import asyncio
import inspect
import math
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from relay.config import settings
from relay.db import Database
from relay.main import app, process_ticket
from relay.ratelimit import (
    RESERVATION_TTL_S,
    client_ip,
    enforce,
    enforce_daily_budget,
    next_utc_midnight,
    release_run,
    reserve_run,
    reset_limits,
    spent_today,
)
from relay.telemetry import record_run


def _request(ip: str | None = "1.2.3.4", headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return _request_raw(ip, raw)


def _request_raw(ip: str | None, raw: list[tuple[bytes, bytes]]) -> Request:
    """Build a request from raw header pairs, so a header can appear twice."""
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "client": (ip, 1234) if ip else None,
    })


# --- reset semantics ---


async def test_reset_limits_clears_consumed_buckets():
    # Tripwire: if MemoryStorage.reset() only pruned expired entries rather than
    # clearing everything, every later test in the suite becomes order-dependent.
    req = _request("10.0.0.1")
    for _ in range(5):
        await enforce("process", "demo", req)
    with pytest.raises(HTTPException):
        await enforce("process", "demo", req)

    await reset_limits()
    await enforce("process", "demo", req)


# --- client ip ---


def test_proxy_header_used_when_trusted(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_header", True)
    req = _request("10.0.0.1", {"Fly-Client-IP": "203.0.113.9"})
    assert client_ip(req) == "203.0.113.9"


def test_proxy_header_ignored_when_untrusted(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_header", False)
    req = _request("10.0.0.1", {"Fly-Client-IP": "203.0.113.9"})
    assert client_ip(req) == "10.0.0.1"


def test_trusted_proxy_falls_back_to_peer_when_header_absent(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_header", True)
    assert client_ip(_request("10.0.0.1")) == "10.0.0.1"


def test_missing_client_resolves_to_unknown():
    assert client_ip(_request(None)) == "unknown"


def test_duplicated_proxy_header_takes_the_last_value(monkeypatch):
    # A client-supplied Fly-Client-IP can only ever arrive ahead of the proxy's
    # own append, so trusting the first occurrence hands the attacker the bucket.
    monkeypatch.setattr(settings, "trust_proxy_header", True)
    req = _request_raw(
        "10.0.0.1",
        [(b"fly-client-ip", b"198.51.100.7"), (b"fly-client-ip", b"203.0.113.9")],
    )
    assert client_ip(req) == "203.0.113.9"


@pytest.mark.parametrize(
    "value",
    [
        "not-an-ip",
        "203.0.113.9/../evil",  # "/" would inject extra segments into the limiter key
        "",
    ],
)
def test_non_ip_proxy_header_falls_back_to_the_peer(monkeypatch, value):
    monkeypatch.setattr(settings, "trust_proxy_header", True)
    req = _request("10.0.0.1", {"Fly-Client-IP": value})
    assert client_ip(req) == "10.0.0.1"


def test_proxy_header_value_is_normalised(monkeypatch):
    # Two spellings of one address must not resolve to two distinct buckets.
    monkeypatch.setattr(settings, "trust_proxy_header", True)
    padded = client_ip(_request("10.0.0.1", {"Fly-Client-IP": " 203.0.113.9 "}))
    compressed = client_ip(_request("10.0.0.1", {"Fly-Client-IP": "2001:0db8::0001"}))
    assert padded == "203.0.113.9"
    assert compressed == "2001:db8::1"


# --- moving window ---


async def test_demo_process_limit_allows_five_then_rejects():
    req = _request("10.0.0.2")
    for _ in range(5):
        await enforce("process", "demo", req)
    with pytest.raises(HTTPException) as exc:
        await enforce("process", "demo", req)
    assert exc.value.status_code == 429


async def test_rejection_carries_retry_after_and_ratelimit_headers():
    req = _request("10.0.0.3")
    for _ in range(5):
        await enforce("process", "demo", req)
    with pytest.raises(HTTPException) as exc:
        await enforce("process", "demo", req)

    headers = exc.value.headers
    assert int(headers["Retry-After"]) >= 1
    assert headers["X-RateLimit-Limit"] == "5"
    assert headers["X-RateLimit-Remaining"] == "0"
    assert int(headers["X-RateLimit-Reset"]) > 0

    detail = exc.value.detail
    assert detail["error"] == "rate_limited"
    assert detail["tier"] == "demo"
    assert "5" in detail["limit"]
    assert detail["retry_after_seconds"] == int(headers["Retry-After"])
    assert "demo" in detail["note"]


async def test_tiers_have_independent_buckets():
    req = _request("10.0.0.4")
    for _ in range(5):
        await enforce("process", "demo", req)
    # The owner allowance is untouched by the exhausted demo bucket.
    await enforce("process", "owner", req)


async def test_buckets_are_independent_per_route():
    req = _request("10.0.0.5")
    for _ in range(5):
        await enforce("process", "demo", req)
    await enforce("create", "demo", req)


async def test_buckets_are_independent_per_ip():
    for _ in range(5):
        await enforce("process", "demo", _request("10.0.0.6"))
    await enforce("process", "demo", _request("10.0.0.7"))


async def test_limits_are_read_from_settings_at_call_time(monkeypatch):
    # Proves the RateLimitItem is built lazily — a limit parsed at import time
    # would ignore this monkeypatch entirely.
    monkeypatch.setattr(settings, "demo_process_limit", "1/hour")
    await reset_limits()
    req = _request("10.0.0.8")
    await enforce("process", "demo", req)
    with pytest.raises(HTTPException) as exc:
        await enforce("process", "demo", req)
    assert exc.value.status_code == 429


# --- daily spend ---


def test_spent_today_is_zero_on_empty_runs_table(conn):
    assert spent_today(conn) == 0.0


def test_spent_today_counts_a_run_recorded_today(conn):
    record_run(conn, ticket_id=1, model="m", duration_ms=10, steps=1,
               input_tokens=1, output_tokens=1, cost_usd=1.25, outcome="send_reply")
    assert spent_today(conn) == pytest.approx(1.25)


def test_spent_today_excludes_yesterdays_runs(conn):
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    # record_run has no created_at parameter, so stamp the row directly.
    conn.execute(
        "INSERT INTO runs (ticket_id, model, duration_ms, steps, input_tokens,"
        " output_tokens, cost_usd, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "m", 10, 1, 1, 1, 4.0, "send_reply", yesterday),
    )
    conn.commit()
    assert spent_today(conn) == 0.0


def test_daily_budget_is_silent_below_the_ceiling(conn, monkeypatch):
    monkeypatch.setattr(settings, "max_daily_cost_usd", 5.0)
    record_run(conn, ticket_id=1, model="m", duration_ms=10, steps=1,
               input_tokens=1, output_tokens=1, cost_usd=1.0, outcome="send_reply")
    enforce_daily_budget(conn)


def test_daily_budget_raises_503_at_the_ceiling(conn, monkeypatch):
    monkeypatch.setattr(settings, "max_daily_cost_usd", 5.0)
    record_run(conn, ticket_id=1, model="m", duration_ms=10, steps=1,
               input_tokens=1, output_tokens=1, cost_usd=5.0, outcome="send_reply")
    with pytest.raises(HTTPException) as exc:
        enforce_daily_budget(conn)

    assert exc.value.status_code == 503
    detail = exc.value.detail
    assert detail["error"] == "daily_budget_exhausted"
    assert detail["spent_usd"] == pytest.approx(5.0)
    assert detail["limit_usd"] == 5.0
    assert detail["resets_at"].endswith("+00:00")
    assert "00:00 UTC" in detail["note"]
    assert int(exc.value.headers["Retry-After"]) >= 1


# --- in-flight reservation ---


def test_reserved_runs_count_toward_todays_spend(conn, monkeypatch):
    monkeypatch.setattr(settings, "max_run_cost_usd", 0.5)
    assert spent_today(conn) == 0.0
    first = reserve_run()
    second = reserve_run()
    assert spent_today(conn) == pytest.approx(1.0)
    release_run(first)
    assert spent_today(conn) == pytest.approx(0.5)
    release_run(second)
    assert spent_today(conn) == 0.0


def test_releasing_an_unknown_token_frees_nothing(conn, monkeypatch):
    # Tokens replaced a bare float precisely so a stray release cannot silently
    # spend another run's claim — the old accumulator just clamped at zero.
    monkeypatch.setattr(settings, "max_run_cost_usd", 0.5)
    token = reserve_run()
    release_run(token + 10_000)
    release_run(None)
    assert spent_today(conn) == pytest.approx(0.5)
    release_run(token)
    assert spent_today(conn) == 0.0


def test_a_claim_keeps_the_amount_it_was_reserved_at(conn, monkeypatch):
    # The claim carries its own amount rather than re-reading the setting, so a
    # value that moves mid-run cannot leave the total drifting up or down.
    monkeypatch.setattr(settings, "max_run_cost_usd", 0.5)
    token = reserve_run()
    monkeypatch.setattr(settings, "max_run_cost_usd", 0.1)
    assert spent_today(conn) == pytest.approx(0.5)
    release_run(token)
    assert spent_today(conn) == 0.0


def test_unreleased_reservation_expires_after_its_ttl(conn, monkeypatch):
    # The failure this closes: Starlette can cancel a streaming response before the
    # generator holding the release ever starts, so the `finally` never runs. Ten
    # such requests used to pin the ceiling shut until the process restarted.
    monkeypatch.setattr(settings, "max_run_cost_usd", 0.5)
    reserve_run()  # token dropped on the floor, as a never-started generator does
    assert spent_today(conn) == pytest.approx(0.5)

    # The clock is injected rather than waited out — the TTL is five minutes.
    assert spent_today(conn, now=time.monotonic() + RESERVATION_TTL_S + 1) == 0.0
    assert spent_today(conn) == 0.0


def test_a_live_reservation_survives_short_of_its_ttl(conn, monkeypatch):
    monkeypatch.setattr(settings, "max_run_cost_usd", 0.5)
    reserve_run()
    assert spent_today(conn, now=time.monotonic() + RESERVATION_TTL_S - 1) == pytest.approx(0.5)


def test_reservations_alone_can_exhaust_the_daily_budget(conn, monkeypatch):
    # The gap closed here: record_run only fires after the SSE stream ends, so
    # concurrent runs would otherwise all read the same stale SUM and all pass.
    monkeypatch.setattr(settings, "max_daily_cost_usd", 1.0)
    monkeypatch.setattr(settings, "max_run_cost_usd", 0.5)
    reserve_run()
    enforce_daily_budget(conn)
    reserve_run()
    with pytest.raises(HTTPException) as exc:
        enforce_daily_budget(conn)
    assert exc.value.status_code == 503


async def test_reset_limits_zeroes_the_reservation(conn):
    reserve_run()
    assert spent_today(conn) > 0.0
    await reset_limits()
    assert spent_today(conn) == 0.0


# --- reset time ---


def test_next_utc_midnight_is_the_next_day_boundary():
    now = datetime(2026, 8, 9, 13, 45, 12, 500, tzinfo=UTC)
    assert next_utc_midnight(now) == datetime(2026, 8, 10, 0, 0, 0, 0, tzinfo=UTC)


def test_next_utc_midnight_is_strictly_in_the_future():
    assert next_utc_midnight() > datetime.now(UTC)


# --- routes ---


DEMO = {"X-API-Key": "test-demo-key"}
TICKET = {
    "customer_email": "ava@acmecorp.com",
    "subject": "Cannot log in",
    "body": "SSO redirect loops forever.",
}


def _process(client, headers: dict[str, str] | None = None):
    """Hit the process gate without ever reaching Claude.

    The gate consumes its unit before the handler looks the ticket up, so an
    unknown id exercises auth, budget and limiter and then stops at 404 — no
    agent loop, no model call, nothing to stub.
    """
    return client.post("/tickets/9999/process", headers=headers)


def _exhaust_budget(conn) -> None:
    record_run(
        conn,
        ticket_id=1,
        model="m",
        duration_ms=1,
        steps=1,
        input_tokens=1,
        output_tokens=1,
        cost_usd=settings.max_daily_cost_usd,
        outcome="send_reply",
    )


def test_demo_tier_defaults_match_d04():
    assert settings.demo_process_limit == "5/hour"
    assert settings.demo_create_limit == "20/hour"


def test_demo_process_limit_429(client):
    for _ in range(5):
        assert _process(client, DEMO).status_code == 404
    resp = _process(client, DEMO)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.headers["X-RateLimit-Limit"] == "5"
    assert resp.headers["X-RateLimit-Remaining"] == "0"
    assert int(resp.headers["X-RateLimit-Reset"]) > 0
    detail = resp.json()["detail"]
    assert detail["error"] == "rate_limited"
    assert detail["limit"] == "5 per 1 hour"
    assert detail["tier"] == "demo"
    assert "cost control" in detail["note"]


def test_demo_create_limit_429(client, monkeypatch):
    # Patched down rather than issuing 21 requests. Limit items are parsed lazily,
    # so this is also the standing proof that lazy construction has not regressed.
    monkeypatch.setattr(settings, "demo_create_limit", "2/hour")
    for _ in range(2):
        assert client.post("/tickets", json=TICKET, headers=DEMO).status_code == 201
    assert client.post("/tickets", json=TICKET, headers=DEMO).status_code == 429


def test_owner_tier_looser(client):
    # Same bucket, well past the demo threshold, on the owner key the fixture sends.
    for _ in range(8):
        assert _process(client).status_code == 404


def test_daily_budget_503(client):
    _exhaust_budget(app.state.conn)
    resp = _process(client)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["error"] == "daily_budget_exhausted"
    assert detail["resets_at"]
    assert "cap is a feature" in detail["note"]


def test_budget_survives_restart(client):
    # The fixture's db_path is a real tmp_path file rather than an in-memory
    # database, so a second TestClient runs lifespan against the same durable
    # state. The ceiling has to come from there, not from process memory.
    _exhaust_budget(app.state.conn)
    with TestClient(app, headers={"X-API-Key": "test-owner-key"}) as restarted:
        assert _process(restarted).status_code == 503


def test_in_flight_reservation(client):
    conn = app.state.conn
    # Driven through the seam rather than a concurrency harness: the requirement is
    # that admitted-but-unfinished runs count against the ceiling, and record_run
    # only fires once the SSE generator ends.
    for _ in range(math.ceil(settings.max_daily_cost_usd / settings.max_run_cost_usd)):
        reserve_run()
    assert _process(client).status_code == 503
    # The whole point: refused before a single row was written.
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_a_stream_that_never_starts_leaks_only_until_the_ttl(client):
    # The real shape of the leak: Starlette can cancel a streaming response before
    # the generator holding release_run() is ever started, and a `finally` in an
    # un-started async generator does not run. Ten of these used to pin the ceiling
    # shut for the life of the process. The body is never iterated here on purpose.
    conn = app.state.conn
    ticket_id = client.post("/tickets", json=TICKET).json()["id"]
    asyncio.run(process_ticket(ticket_id))

    assert spent_today(conn) == pytest.approx(settings.max_run_cost_usd)
    assert spent_today(conn, now=time.monotonic() + RESERVATION_TTL_S + 1) == 0.0


def test_rate_limit_and_budget_ordering(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_process_limit", "1/hour")
    _exhaust_budget(app.state.conn)
    # The budget is global, so it is checked first: an outage must not also burn
    # the caller's per-IP allowance. Kept as-is deliberately — the ordering is the
    # property, and the refusal is metered on its own bucket instead (below), so
    # this assertion did not have to be traded away to throttle the 503 path.
    for _ in range(3):
        assert _process(client, DEMO).status_code == 503

    monkeypatch.setattr(settings, "max_daily_cost_usd", 1000.0)
    assert _process(client, DEMO).status_code == 404
    assert _process(client, DEMO).status_code == 429


def test_a_budget_outage_is_still_throttled(client, monkeypatch):
    # WR-02. The ceiling raises before the tiered window is consumed, so during an
    # outage /process was unthrottled at its own tier: anyone holding the published
    # demo key could hold it open at the anon 60/minute, and every one of those
    # requests still ran the daily SUM and took Database's lock. The service was
    # least defended exactly when it was already degraded. The refusal now charges a
    # bucket of its own on the way out.
    monkeypatch.setattr(settings, "outage_process_limit", "3/minute")
    _exhaust_budget(app.state.conn)

    for _ in range(3):
        assert _process(client, DEMO).status_code == 503
    assert _process(client, DEMO).status_code == 429, (
        "a budget outage left /process unthrottled at its own tier"
    )


def test_the_outage_bucket_does_not_spend_the_callers_own_allowance(client, monkeypatch):
    # The property the ordering exists to protect, and the reason the outage path got
    # its own bucket rather than a swap: a global outage is not the caller's fault, so
    # it must not cost them runs they can use once the ceiling resets at 00:00 UTC.
    monkeypatch.setattr(settings, "demo_process_limit", "2/hour")
    monkeypatch.setattr(settings, "outage_process_limit", "10/minute")
    _exhaust_budget(app.state.conn)

    for _ in range(5):
        assert _process(client, DEMO).status_code == 503

    monkeypatch.setattr(settings, "max_daily_cost_usd", 1000.0)
    # Both tier units still unspent — the five refusals above charged the other bucket.
    assert _process(client, DEMO).status_code == 404
    assert _process(client, DEMO).status_code == 404
    assert _process(client, DEMO).status_code == 429


def test_the_budget_readers_annotate_what_connect_actually_returns():
    # connect() has returned a Database since phase 2, but these two kept annotating
    # sqlite3.Connection — and the audit that swept the other modules missed this file
    # because nothing asserted the pairing. Cheap to keep honest, and the annotations
    # are the only documentation of what these functions accept: a `Database` is not a
    # `sqlite3.Connection` and has no cursor(), executescript-on-cursor or context
    # manager, so a reader who trusts the hint writes code that does not run.
    for fn in (spent_today, enforce_daily_budget):
        assert inspect.get_annotations(fn)["conn"] is Database, (
            f"{fn.__name__} does not annotate the type connect() hands it"
        )
