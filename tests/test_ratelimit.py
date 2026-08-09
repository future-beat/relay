import math
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from relay.config import settings
from relay.main import app
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


def test_rate_limit_and_budget_ordering(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_process_limit", "1/hour")
    _exhaust_budget(app.state.conn)
    # The budget is global, so it is checked first: an outage must not also burn
    # the caller's per-IP allowance.
    for _ in range(3):
        assert _process(client, DEMO).status_code == 503

    monkeypatch.setattr(settings, "max_daily_cost_usd", 1000.0)
    assert _process(client, DEMO).status_code == 404
    assert _process(client, DEMO).status_code == 429
