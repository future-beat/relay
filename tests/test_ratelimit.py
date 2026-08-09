from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from relay.config import settings
from relay.ratelimit import (
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


@pytest.fixture(autouse=True)
async def _reset_limits():
    await reset_limits()
    yield


def _request(ip: str | None = "1.2.3.4", headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
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
    reserve_run()
    reserve_run()
    assert spent_today(conn) == pytest.approx(1.0)
    release_run()
    assert spent_today(conn) == pytest.approx(0.5)
    release_run()
    assert spent_today(conn) == 0.0


def test_releasing_more_than_reserved_clamps_at_zero(conn):
    release_run()
    assert spent_today(conn) == 0.0


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
