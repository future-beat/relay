import json
import logging

import pytest
from fastapi.testclient import TestClient

from helpers import FakeClient, response, text_block, tool_use_block
from relay.main import app
from relay.telemetry import JsonFormatter, run_metrics


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from relay.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    with TestClient(app) as client:
        yield client


def _make_ticket(client) -> int:
    created = client.post(
        "/tickets",
        json={
            "customer_email": "liam@brightco.io",
            "subject": "API limits",
            "body": "What are my rate limits?",
        },
    )
    return created.json()["id"]


def test_process_streams_and_records_run(client):
    ticket_id = _make_ticket(client)
    app.state.client = FakeClient([
        response([
            tool_use_block("send_reply", {
                "ticket_id": ticket_id,
                "body": "Hi Liam — your Pro plan allows 600 requests/minute per workspace.",
            }),
        ]),
        response([text_block("Done.")], stop_reason="end_turn"),
    ])

    resp = client.post(f"/tickets/{ticket_id}/process")
    assert resp.status_code == 200
    assert "event: resolution" in resp.text
    assert "event: done" in resp.text

    metrics = client.get("/metrics").json()
    assert metrics["runs"] == 1
    assert metrics["outcomes"] == {"send_reply": 1}
    assert metrics["tokens"]["input"] == 2000
    assert metrics["cost_usd"]["total"] > 0
    assert metrics["last_runs"][0]["ticket_id"] == ticket_id


def test_failed_run_recorded_with_error_outcome(client):
    ticket_id = _make_ticket(client)
    app.state.client = FakeClient([
        response([text_block("Hmm, no action needed.")], stop_reason="end_turn"),
    ])
    client.post(f"/tickets/{ticket_id}/process")
    metrics = client.get("/metrics").json()
    assert metrics["outcomes"] == {"error:ended_without_action": 1}


def test_metrics_empty_state(client):
    metrics = client.get("/metrics").json()
    assert metrics["runs"] == 0
    assert metrics["latency_ms"]["p50"] == 0


def test_percentiles(conn):
    from relay.telemetry import record_run

    for ms in [100, 200, 300, 400, 1000]:
        record_run(conn, ticket_id=1, model="m", duration_ms=ms, steps=1,
                   input_tokens=1, output_tokens=1, cost_usd=0.01, outcome="send_reply")
    metrics = run_metrics(conn)
    assert metrics["latency_ms"]["p50"] == 300
    assert metrics["latency_ms"]["p95"] == 1000
    assert metrics["latency_ms"]["max"] == 1000


def test_dashboard_served(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Relay" in resp.text


def test_json_log_formatter_includes_context():
    record = logging.LogRecord("relay.agent", logging.INFO, __file__, 1, "tool.executed",
                               None, None)
    record.ctx = {"ticket_id": 7, "tool": "search_docs"}
    line = json.loads(JsonFormatter().format(record))
    assert line["event"] == "tool.executed"
    assert line["ticket_id"] == 7
    assert line["level"] == "info"
