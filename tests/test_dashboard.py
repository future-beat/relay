"""Phase 6 (dashboard experience) tests: the data foundations the later waves read.

Its own module on purpose. tests/test_run_events.py is Phase 5's file and is already
2127 lines; the surfaces covered here — the guarded column migrations, RunRecorder's
millisecond stamping, and the single budget arithmetic D-11 makes the gauge and the
gate share — are Phase 6's, and belong where a reader looking for Phase 6 will find them.
"""

import asyncio
import inspect
import json
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from helpers import FakeClient, response, text_block, tool_use_block
from relay import db as db_module
from relay.agent import _execute_guarded
from relay.config import settings
from relay.db import connect, init_db
from relay.events import project_run_detail
from relay.guardrails import ToolPolicy
from relay.main import app
from relay.ratelimit import (
    _LIMIT_SETTINGS,
    _limit_item,
    budget_snapshot,
    enforce_daily_budget,
    release_run,
    reserve_run,
    spent_today,
)
from relay.retrieval import normalise_citation
from relay.telemetry import record_run


def _create_table_block(table: str) -> str:
    """The CREATE TABLE body for one table, straight out of db.SCHEMA."""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\);", db_module.SCHEMA, re.DOTALL
    )
    assert match, f"no CREATE TABLE block for {table} in db.SCHEMA"
    return match.group(1)


def test_phase6_migrations_are_idempotent(tmp_path):
    """A second init_db on a POPULATED db adds both new columns without raising.

    File-backed, not the `conn` fixture: a :memory: DB is always fresh, so it never
    exercises the "table already exists" path this test is entirely about — which is
    the only path the live Fly volume takes.

    MUTATION that must turn this red: drop the PRAGMA guard inside
    `_add_column_if_missing` and ALTER unconditionally — the second init_db raises
    sqlite3.OperationalError: duplicate column name: elapsed_ms.

    SECOND MUTATION, covered by the source assertion at the end: add
    `elapsed_ms INTEGER` to the run_events CREATE TABLE in SCHEMA and delete the ALTER.
    A fresh DB stays green and production silently gets no column (D-13), so behaviour
    alone cannot catch it. The assertion is therefore on the source: the DDL must name
    neither new column, so fresh and existing databases take the same migration path.
    """
    db = connect(tmp_path / "relay.db")
    try:
        init_db(db)
        # Rows written by code that predates these columns — i.e. what is on the volume.
        db.execute(
            "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
            ("ava@acmecorp.com", "legacy", "written before origin existed"),
        )
        db.execute(
            "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            ("legacyuid", 1, 1, "tool_use", "{}"),
        )
        db.commit()

        init_db(db)  # the re-run that happens on every boot against the live volume

        run_event_cols = {r["name"] for r in db.execute("PRAGMA table_info(run_events)")}
        ticket_cols = {r["name"] for r in db.execute("PRAGMA table_info(tickets)")}
        run_cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
        assert "elapsed_ms" in run_event_cols
        assert "origin" in ticket_cols
        # The migration this generalises must still happen — the helper replaced it.
        assert "run_uid" in run_cols

        # Legacy rows survive and read NULL. For `origin` that is load-bearing, not
        # incidental: NULL means "not demo-originated" and Wave 3 redacts it (D-02).
        assert db.execute("SELECT elapsed_ms FROM run_events").fetchone()["elapsed_ms"] is None
        assert db.execute("SELECT origin FROM tickets").fetchone()["origin"] is None
    finally:
        db.close()

    # Source assertion (see SECOND MUTATION above): the DDL owns neither column.
    assert "origin" not in _create_table_block("tickets")
    assert "elapsed_ms" not in _create_table_block("run_events")


class SlowFakeClient(FakeClient):
    """FakeClient with a real pause between turns, so a run spans measurable time.

    Without it a scripted run finishes inside one millisecond and every elapsed_ms is
    legitimately 0 — which would make the "at least one non-zero" assertion below
    vacuous and blind to the origin-per-insert mutation named in the test's docstring.
    """

    async def _create(self, **kwargs):
        await asyncio.sleep(0.02)
        return await super()._create(**kwargs)


def _make_ticket(client, email: str, body: str) -> int:
    created = client.post(
        "/tickets", json={"customer_email": email, "subject": "API limits", "body": body}
    )
    assert created.status_code == 201
    return created.json()["id"]


def test_run_events_carry_elapsed_ms(client, capture_frames, monkeypatch):
    """Every persisted row carries a millisecond offset from its run's start.

    Both recorder paths are covered: the read tool's tool_result goes through
    RunRecorder.record (its own transaction) and the write tool's through
    execute_and_record (inside the tool's transaction), and the assertions below name
    each by its payload rather than trusting that one implies the other.

    MUTATION that must turn this red: drop `elapsed_ms` from the INSERT column list and
    its value from the tuple in `_insert_event` — every row reads NULL.

    SECOND, INDEPENDENT MUTATION: move `self._t0 = time.monotonic()` from `__init__`
    into `_insert_event` — every row then measures from its own insert and reads 0, so
    the "at least one non-zero" assertion fails. That is the mutation that actually
    pins the origin being PER RUN, which is what makes
    elapsed_ms(tool_result) - elapsed_ms(tool_use) a tool's wall time.
    """
    # A real VOYAGE_API_KEY in .env would otherwise make retrieval reachable from this
    # suite; pinned so the test is free by construction, not by interception.
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    app.state.client = SlowFakeClient([
        response([tool_use_block("lookup_customer", {"email": "liam@brightco.io"})]),
        response([tool_use_block("send_reply", {"ticket_id": ticket_id, "body": "z" * 40})]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    body, _frames = asyncio.run(capture_frames(ticket_id))
    assert "event: error" not in body

    # Re-opened from disk: the claim is that these rows are COMMITTED.
    reopened = connect(settings.db_path)
    try:
        run_uid = reopened.execute("SELECT run_uid FROM runs").fetchone()["run_uid"]
        rows = reopened.execute(
            "SELECT seq, type, payload, elapsed_ms FROM run_events WHERE run_uid = ?"
            " ORDER BY seq",
            (run_uid,),
        ).fetchall()
    finally:
        reopened.close()

    assert rows, "the run persisted no events at all"
    elapsed = [r["elapsed_ms"] for r in rows]
    assert all(isinstance(v, int) for v in elapsed), f"a row was not stamped: {elapsed}"
    # Monotonic origin, so time cannot run backwards between two rows of one run.
    assert elapsed == sorted(elapsed), elapsed
    # An offset from the run's start, not a wall clock or an epoch.
    assert elapsed[0] < 5000, elapsed[0]
    # Two SlowFakeClient turns precede the last row, so it cannot legitimately be 0.
    assert max(elapsed) > 0, elapsed

    by_tool = {
        json.loads(r["payload"])["tool"]: r["elapsed_ms"]
        for r in rows
        if r["type"] == "tool_result"
    }
    # record() — the read tool's own transaction.
    assert isinstance(by_tool.get("lookup_customer"), int)
    # execute_and_record() — inside the write tool's transaction, the path a stamp
    # added at the record() call site instead of in _insert_event would have missed.
    assert isinstance(by_tool.get("send_reply"), int)
    assert by_tool["send_reply"] >= by_tool["lookup_customer"]


def test_budget_snapshot_and_the_gate_cannot_disagree(conn, monkeypatch):
    """One arithmetic, two consumers (D-11): the gauge's number IS the gate's number.

    The /metrics gauge renders what enforce_daily_budget refuses on. Two producers
    means the page can show budget remaining while the gate is already refusing —
    on a page whose entire purpose is credibility.

    MUTATION that must turn this red: give budget_snapshot its own
    `SELECT COALESCE(SUM(cost_usd), 0.0) FROM runs WHERE ...` instead of calling
    spent_today. The reservation below then vanishes from the snapshot while the gate
    still counts it, and the equality against spent_today() fails. That equality is
    what does the work here — asserting the 503 body matches the snapshot alone would
    stay green under the mutation, because the rewritten gate reads the same (wrong)
    snapshot.
    """
    monkeypatch.setattr(settings, "max_daily_cost_usd", 5.0)
    monkeypatch.setattr(settings, "max_run_cost_usd", 0.5)
    record_run(conn, ticket_id=1, model="m", duration_ms=10, steps=1,
               input_tokens=1, output_tokens=1, cost_usd=1.0, outcome="send_reply")

    # A run admitted but not yet written to `runs`. The gate compares against this, so
    # a gauge that summed only committed rows would read 0.5 low and promise a visitor
    # budget the very next request refuses.
    token = reserve_run()
    try:
        snap = budget_snapshot(conn)
        assert snap["spent_today_usd"] == round(spent_today(conn), 4) == 1.5
        assert snap["daily_ceiling_usd"] == 5.0
        assert snap["remaining_usd"] == 3.5
        assert snap["exhausted"] is False
        # A JSON-serialisable string for the route, and a real instant: the 503 path
        # parses this back rather than re-deriving midnight on its own clock.
        assert snap["resets_at"].endswith("+00:00")
        assert datetime.fromisoformat(snap["resets_at"]).hour == 0

        # Below the ceiling the gate is silent, from the same numbers.
        enforce_daily_budget(conn)

        record_run(conn, ticket_id=2, model="m", duration_ms=10, steps=1,
                   input_tokens=1, output_tokens=1, cost_usd=4.0, outcome="send_reply")
        exhausted = budget_snapshot(conn)
        assert exhausted["exhausted"] is True
        assert exhausted["remaining_usd"] == 0.0  # floored, never negative

        with pytest.raises(HTTPException) as exc:
            enforce_daily_budget(conn)
    finally:
        release_run(token)

    assert exc.value.status_code == 503
    detail = exc.value.detail
    # The three keys tests/test_ratelimit.py asserts by name, unmoved — and each one
    # EXACTLY the snapshot's, not a rounding of the same float computed twice.
    assert detail["spent_usd"] == exhausted["spent_today_usd"]
    assert detail["limit_usd"] == exhausted["daily_ceiling_usd"]
    assert detail["resets_at"] == exhausted["resets_at"]
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_run_detail_limit_bucket_resolves(conn):
    """The Wave-3 drill-down's bucket exists before the route that reads it.

    _LIMIT_SETTINGS is a hard KeyError inside _limit_item, not a default: a missing
    entry is a 500 on the new route's FIRST request, in production, with no local
    signal. So the bucket and its settings attribute land in the same commit.

    MUTATION that must turn this red: delete the ("run_detail", "anon") entry from
    _LIMIT_SETTINGS — _limit_item raises KeyError.
    """
    item = _limit_item("run_detail", "anon")
    assert item.amount >= 1

    # Deliberately NOT the events bucket: a drill-down flood must not spend the live
    # feed's reconnect allowance and silently break the feed for that visitor.
    assert (
        _LIMIT_SETTINGS[("run_detail", "anon")] != _LIMIT_SETTINGS[("events", "anon")]
    )
    assert settings.run_detail_max_events > 0
    assert settings.metrics_window_days > 0


# --- Wave 2: the drill-down redactor ------------------------------------------------

CITED_TICKET = {
    "id": 4101,
    "customer_email": "cite@brightco.io",
    "subject": "refunds",
    "body": "how do refunds work?",
}
GROUNDED_REPLY = "Refunds are issued to the original payment method within 14 days."


def _seed_ticket(conn, ticket=CITED_TICKET) -> int:
    conn.execute(
        "INSERT INTO tickets (id, customer_email, subject, body) VALUES (?, ?, ?, ?)",
        (ticket["id"], ticket["customer_email"], ticket["subject"], ticket["body"]),
    )
    conn.commit()
    return ticket["id"]


def test_normalise_citation_is_the_guards_normalisation(conn, registry):
    """One normalisation, shared by the citation guard and the drill-down's highlight.

    Two halves. (1) The helper itself: whitespace and case are the only things it
    removes, because every retrieved id is already a lowercase filename plus a slug.
    (2) The substitution changed no behaviour — the guard, driven through
    `_execute_guarded` with a citation that differs from a retrieved id only in case
    and surrounding whitespace, still ACCEPTS and the reply still lands. Without (2)
    the helper could be correct in isolation while the guard quietly kept its own
    open-coded copy, which is the drift this exists to make impossible.

    MUTATION that must turn this red: make normalise_citation return `value`
    unchanged. The case-differing citation is then absent from the guard's accept-set,
    the call is denied with `denied_by: "citation"`, and (2)'s acceptance fails.
    """
    assert normalise_citation(" API.md#Rate-Limits ") == "api.md#rate-limits"
    # Idempotent, so the drill-down may normalise an already-normalised id.
    assert normalise_citation("api.md#rate-limits") == "api.md#rate-limits"

    ticket_id = _seed_ticket(conn)
    retrieved_ids = {"billing.md", "billing.md#refunds"}
    result, is_error = _execute_guarded(
        registry["send_reply"],
        "send_reply",
        {
            "ticket_id": ticket_id,
            "body": GROUNDED_REPLY,
            # Same id the run retrieved, retyped by the model with different case and
            # stray whitespace — a formatting difference, not a fabricated source.
            "citations": ["  BILLING.md#Refunds\n"],
        },
        ToolPolicy(),
        bound_ticket_id=ticket_id,
        retrieved_ids=retrieved_ids,
    )
    assert is_error is False, result
    assert json.loads(result)["status"] == "resolved"

    # And the guard still denies a source this run never retrieved — the acceptance
    # above is a normalisation, not a hole.
    denied, denied_is_error = _execute_guarded(
        registry["send_reply"],
        "send_reply",
        {
            "ticket_id": ticket_id,
            "body": GROUNDED_REPLY,
            "citations": ["api.md#rate-limits"],
        },
        ToolPolicy(),
        bound_ticket_id=ticket_id,
        retrieved_ids=retrieved_ids,
    )
    assert denied_is_error is True
    assert json.loads(denied)["denied_by"] == "citation"


# Sentinels for the drill-down. Distinct per field so a failure names WHICH secret got
# out and through which step, and implausible-by-accident so absence means something.
DETAIL_EMAIL = "drilldown-leak-3e91@example.com"      # lookup_customer input + result
DETAIL_QUERY = "sk-ant-DRILLDOWN-FAKE-KEY-77af"       # search_docs input.query
DETAIL_PROSE = "DRILLDOWN-KB-PROSE-4c8e"              # search_docs result text + heading
DETAIL_REASON = "DRILLDOWN-TICKET-BODY-9b12"          # create_escalation input.reason
DETAIL_REPLY = "DRILLDOWN-REPLY-TEXT-1d55"            # send_reply input.body
DETAIL_CITE = "DRILLDOWN-FABRICATED-CITE-6a20"        # guardrail missing_citations
DETAIL_ERROR = "DRILLDOWN-ERROR-MESSAGE-8f74"         # tool_result error string

DETAIL_SENTINELS = (
    ("customer email", DETAIL_EMAIL),
    ("search query", DETAIL_QUERY),
    ("retrieved prose", DETAIL_PROSE),
    ("escalation reason", DETAIL_REASON),
    ("reply body", DETAIL_REPLY),
    ("missing citation", DETAIL_CITE),
    ("tool error message", DETAIL_ERROR),
)

DETAIL_UID = "drilldown-uid"


def _known_tools(registry) -> dict[str, frozenset[str]]:
    """Each registered tool's DECLARED argument keys, straight off its Claude schema.

    Built from the real registry rather than a literal so a schema change moves the
    clamp with it — a hardcoded set would keep passing while the projector started
    excluding a key the tool genuinely declares.
    """
    return {
        name: frozenset(spec.schema["input_schema"]["properties"])
        for name, spec in registry.items()
    }


def _store(conn, events, *, run_uid: str = DETAIL_UID, ticket_id: int = 77) -> list:
    """Write raw run_events rows and read them back as real sqlite3.Rows.

    `events` is a list of (type, payload, elapsed_ms); a payload that is already a str
    is written through untouched, which is how the malformed-JSON case is built.
    """
    for seq, (type_, payload, elapsed_ms) in enumerate(events, start=1):
        conn.execute(
            "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload, elapsed_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_uid,
                ticket_id,
                seq,
                type_,
                payload if isinstance(payload, str) else json.dumps(payload, default=str),
                elapsed_ms,
            ),
        )
    conn.commit()
    return conn.execute(
        "SELECT seq, type, payload, elapsed_ms, created_at FROM run_events"
        " WHERE run_uid = ? ORDER BY seq",
        (run_uid,),
    ).fetchall()


def _leaky_run_events() -> list:
    """One run carrying every sentinel, in the raw shapes RunRecorder actually writes."""
    return [
        ("usage", {"steps": 1, "input_tokens": 900, "output_tokens": 120,
                   "cost_usd": 0.004, "max_cost_usd": 0.5}, 10),
        ("text", {"text": f"The customer wrote: {DETAIL_REASON}"}, 20),
        ("tool_use", {"tool": "lookup_customer", "input": {"email": DETAIL_EMAIL}}, 30),
        ("tool_result", {"tool": "lookup_customer", "is_error": False, "result": {
            "found": True,
            "customer": {"email": DETAIL_EMAIL, "name": "Leak", "plan": "enterprise"},
            "recent_tickets": [{"id": 1, "subject": DETAIL_REASON, "status": "open"}],
        }}, 55),
        ("tool_use", {"tool": "search_docs", "input": {"query": DETAIL_QUERY}}, 60),
        ("tool_result", {"tool": "search_docs", "is_error": False, "result": {
            "results": [{
                "doc": "billing.md", "heading": f"Refunds {DETAIL_PROSE}",
                "id": "billing.md#refunds",
                "anchors": ["billing.md", "billing.md#refunds"],
                "text": f"Refunds take 14 days. {DETAIL_PROSE}", "score": 0.82,
            }],
            "retrieval_mode": "hybrid", "degraded": False,
        }}, 140),
        ("notice", {"kind": "retrieval_degraded", "tool": "search_docs",
                    "retrieval_mode": "keyword", "cause": "no_index", "results": 1}, 145),
        ("guardrail", {"guard": "citation", "tool": "send_reply",
                       "missing_citations": [DETAIL_CITE],
                       "retrieved_ids": ["billing.md#refunds"], "action": "denied"}, 150),
        ("tool_result", {"tool": "send_reply", "is_error": True, "result": {
            "error": f"citation(s) ['{DETAIL_CITE}'] were not retrieved. {DETAIL_ERROR}",
            "denied_by": "citation", "missing_citations": [DETAIL_CITE],
        }}, 155),
        ("guardrail", {"guard": "ticket_binding", "tool": "send_reply",
                       "expected_ticket_id": 77, "supplied_ticket_id": 999,
                       "action": "denied"}, 160),
        ("tool_use", {"tool": "create_escalation", "input": {
            "ticket_id": 77, "reason": DETAIL_REASON, "priority": "high"}}, 170),
        ("tool_use", {"tool": "send_reply", "input": {
            "ticket_id": 77, "body": DETAIL_REPLY,
            "citations": ["  BILLING.md#Refunds "]}}, 180),
        ("tool_result", {"tool": "send_reply", "is_error": False,
                         "result": {"reply_id": 5, "status": "resolved"}}, 240),
        ("resolution", {"via": "send_reply", "cost_usd": 0.012, "steps": 4,
                        "input_tokens": 900, "output_tokens": 120}, 250),
        ("error", {"reason": "api_error", "status": 529, "type": "overloaded_error"}, 260),
    ]


def test_project_run_detail_publishes_only_named_fields(conn, registry):
    """No sentinel survives the public branch — asserted per step and per secret.

    The rows are hand-written in the exact shapes RunRecorder persists, with a distinct
    secret in every sensitive position the payload map enumerates: lookup_customer's
    input.email AND its result.customer.email, search_docs' input.query and its result
    text/heading, create_escalation's reason, send_reply's body, a guardrail's
    missing_citations, and a denied tool_result's `error` string (Pitfall 7 — the
    drill-down must not "improve" on _project_tool_result's dropping of that message).

    The anti-vacuity half matters as much as the absence half: a projector that
    published nothing at all would leak nothing at all. So the step list must be
    non-empty and must actually contain the tool_use, tool_result and guardrail steps
    the secrets rode in on, and each raw payload must still hold its sentinel (D-01:
    run_events stays full-fidelity; redaction happens on the way out, or phase 6 has
    nothing to drill into).

    MUTATION that must turn this red for ALL sentinels: forward the raw payload on the
    public branch — `step = dict(json.loads(row["payload"])); step["seq"] = row["seq"]`.

    SECOND, INDEPENDENT MUTATION: give `full_fidelity` a default of True, so a caller
    that forgets to decide gets full disclosure (T-06-11). Caught by the SIGNATURE
    assertion below and not by the behaviour above — every call in this file passes the
    flag explicitly, so no amount of leak-checking can see a default. Stated plainly:
    that assertion is a regression guard on the signature, not proof about output.
    """
    # T-06-11: keyword-only, no default. A positional bool is the kind of argument a
    # future route passes by accident; a default is the kind of decision a future
    # caller never makes at all.
    param = inspect.signature(project_run_detail).parameters["full_fidelity"]
    assert param.default is inspect.Parameter.empty, "full_fidelity acquired a default"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY

    rows = _store(conn, _leaky_run_events())

    steps = project_run_detail(
        rows, full_fidelity=False, known_tools=_known_tools(registry)
    )

    # Anti-vacuity: the secrets were genuinely carried, and the projector genuinely
    # produced the steps they rode in on.
    raw = "".join(r["payload"] for r in rows)
    for name, sentinel in DETAIL_SENTINELS:
        assert sentinel in raw, f"the {name} sentinel never reached run_events"
    assert steps, "the projector returned nothing — the absence assertions would be vacuous"
    by_type = {s["type"] for s in steps}
    assert {"tool_use", "tool_result", "guardrail", "text", "notice"} <= by_type, by_type
    assert {s.get("tool") for s in steps if s["type"] == "tool_use"} == {
        "lookup_customer", "search_docs", "create_escalation", "send_reply"
    }

    # Collected rather than asserted in place: under the mutations above the useful
    # answer is EVERY step/secret pair that opened, not just the first.
    leaks = [
        (name, step["seq"], step["type"])
        for step in steps
        for name, sentinel in DETAIL_SENTINELS
        if sentinel in json.dumps(step, default=str)
    ]
    assert leaks == [], f"the public drill-down leaked: {leaks}"

    # Pitfall 7 stated directly: no error/message key exists on the public branch at all.
    assert not any({"error", "message"} & set(step) for step in steps), steps


def test_project_run_detail_demo_branch_adds_only_named_fields(conn, registry):
    """D-02's inverse: full fidelity really is fuller, and still only where named.

    Without this half, `project_run_detail` could regress to redacted-for-everyone and
    every leak assertion in this module would stay green — the Try-it payoff would just
    silently stop working. So the demo branch must return the raw `input`, `result`,
    `text` and `missing_citations`, and NOTHING outside that named list: the exact key
    set of a tool_use step is asserted in BOTH branches, so a demo branch built as a
    raw spread fails here rather than being caught only by review.
    """
    rows = _store(conn, _leaky_run_events())
    known = _known_tools(registry)

    public = project_run_detail(rows, full_fidelity=False, known_tools=known)
    demo = project_run_detail(rows, full_fidelity=True, known_tools=known)

    demo_json = json.dumps(demo, default=str)
    for name, sentinel in (
        ("customer email", DETAIL_EMAIL),        # tool_use.input + tool_result.result
        ("search query", DETAIL_QUERY),
        ("retrieved prose", DETAIL_PROSE),
        ("escalation reason", DETAIL_REASON),
        ("reply body", DETAIL_REPLY),
        ("missing citation", DETAIL_CITE),
    ):
        assert sentinel in demo_json, f"the demo branch dropped the {name} — D-02 regressed"

    def _tool_use(steps, tool):
        return next(s for s in steps if s["type"] == "tool_use" and s.get("tool") == tool)

    assert set(_tool_use(public, "send_reply")) == {
        "seq", "type", "elapsed_ms", "tool", "arg_keys", "unknown_arg_count"
    }
    assert set(_tool_use(demo, "send_reply")) == {
        "seq", "type", "elapsed_ms", "tool", "arg_keys", "unknown_arg_count", "input"
    }

    def _guardrail(steps, guard):
        return next(s for s in steps if s["type"] == "guardrail" and s["guard"] == guard)

    assert set(_guardrail(public, "citation")) == {
        "seq", "type", "elapsed_ms", "guard", "tool", "action",
        "expected_ticket_id", "supplied_ticket_id", "missing_count",
    }
    assert set(_guardrail(demo, "citation")) - set(_guardrail(public, "citation")) == {
        "missing_citations"
    }
    # `retrieved_ids` is on neither branch: it is not in the allowlist table, and a
    # field nobody wrote down is not published even when it looks harmless.
    assert "retrieved_ids" not in _guardrail(demo, "citation")

    text_public = next(s for s in public if s["type"] == "text")
    text_demo = next(s for s in demo if s["type"] == "text")
    assert set(text_public) == {"seq", "type", "elapsed_ms", "char_count"}
    assert set(text_demo) - set(text_public) == {"text"}
    assert text_public["char_count"] == len(f"The customer wrote: {DETAIL_REASON}")


def test_project_run_detail_drops_unknown_and_malformed(conn, registry):
    """Fail-closed on both axes: an unknown type and an unparseable payload are DROPPED.

    Same default as project(): a new yield site in agent.py is absent from the
    drill-down until someone adds it here on purpose. And a malformed payload is a
    dropped step, never a 500 — load_index's degrade-and-log posture, because one bad
    row must not make a whole run's history unreadable.

    MUTATION that must turn this red: replace the `return None` fallthrough with
    `return dict(payload)` — the debug_dump row's secret then appears in the output.
    SECOND MUTATION: let json.loads raise instead of catching — the malformed row
    raises out of the projector and this test errors rather than asserting.
    """
    rows = _store(conn, [
        ("usage", {"steps": 1, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.1}, 5),
        ("debug_dump", {"secret": DETAIL_EMAIL}, 6),
        ("tool_use", "{not json at all", 7),
        ("tool_use", "[1, 2, 3]", 8),  # valid JSON, wrong shape — a payload is a dict
        ("resolution", {"via": "send_reply", "cost_usd": 0.1, "steps": 1}, 9),
    ])

    steps = project_run_detail(
        rows, full_fidelity=False, known_tools=_known_tools(registry)
    )

    assert [s["type"] for s in steps] == ["usage", "resolution"]
    assert [s["seq"] for s in steps] == [1, 5]
    assert DETAIL_EMAIL not in json.dumps(steps, default=str)

    # And the demo branch drops them too: full fidelity widens named fields, it does
    # not turn off the type allowlist.
    demo = project_run_detail(rows, full_fidelity=True, known_tools=_known_tools(registry))
    assert [s["type"] for s in demo] == ["usage", "resolution"]


def test_project_run_detail_survives_a_swept_run(conn, registry):
    """A run whose events the 30-day retention deleted projects to [], not a crash.

    purge_expired_run_events spares the `runs` row on purpose (db.py), so this state is
    reachable in production for every run older than the retention window. The route
    renders it as `status: "swept"`; the projector's job is simply not to be the reason
    that page 500s.
    """
    assert project_run_detail([], full_fidelity=False, known_tools=_known_tools(registry)) == []
    assert project_run_detail([], full_fidelity=True, known_tools=_known_tools(registry)) == []


def test_tool_use_arg_keys_are_clamped(conn, registry):
    """INFO-1 for this surface: neither the tool NAME nor an argument KEY is model-free.

    Both are strings the model chose and both reach a browser. The name is clamped to
    the registry and an unregistered one renders the literal "unknown"; argument keys
    are intersected with the tool's own declared input_schema.properties, and whatever
    that excludes becomes a COUNT — a number, which cannot carry a payload.

    MUTATION that must turn this red: `arg_keys = sorted(raw_input)` — the injected key
    name appears verbatim in the output and unknown_arg_count reads 0.
    """
    rows = _store(conn, [
        ("tool_use", {"tool": "send_reply", "input": {
            "ticket_id": 77,
            "body": "z" * 40,
            # A model-chosen key the tool never declared, carrying a payload.
            f"<img src=x onerror={DETAIL_EMAIL}>": "x",
            # Real to the EXECUTOR but absent from the schema the model was shown, so
            # the clamp follows the declared properties and not the Python signature.
            "max_results": 3,
        }}, 10),
        ("tool_use", {"tool": "delete_everything", "input": {"target": "prod"}}, 20),
    ])

    steps = project_run_detail(
        rows, full_fidelity=False, known_tools=_known_tools(registry)
    )

    declared, unknown = steps
    assert declared["tool"] == "send_reply"
    assert declared["arg_keys"] == ["body", "ticket_id"]  # sorted, declared only
    assert declared["unknown_arg_count"] == 2
    assert DETAIL_EMAIL not in json.dumps(steps, default=str)

    # An unregistered tool is named "unknown", not echoed — and since it declares
    # nothing, all of its arguments are unknown.
    assert unknown["tool"] == "unknown"
    assert unknown["arg_keys"] == []
    assert unknown["unknown_arg_count"] == 1
    assert "delete_everything" not in json.dumps(steps, default=str)


def test_steps_carry_seq_and_elapsed_and_tool_durations(conn, registry):
    """The envelope: seq, elapsed_ms, a real per-tool duration — and never created_at.

    `duration_ms` is elapsed_ms(tool_result) - elapsed_ms(the paired tool_use), which is
    exactly what RunRecorder's stamping was built to make subtractable. Pairing is by
    tool name to the nearest preceding unpaired tool_use, so two interleaved calls to
    different tools do not swap durations.

    MUTATION that must turn this red: pair each tool_result with the immediately
    preceding tool_use row regardless of name — the LIFO run below then reads
    lookup_customer 105 and search_docs 155 instead of 200 and 155.
    SECOND MUTATION, and the reason the FIFO run exists: pair name-blind but still pop
    (a single global stack). That survives the LIFO ordering, where popping happens to
    restore the right answer, and it is the FIFO ordering that kills it —
    lookup_customer reads 40 and search_docs 90 instead of 50 and 80. One ordering was
    not enough; I ran the first version of this test against that mutation and it
    passed.
    THIRD MUTATION: publish `created_at` in the envelope — the last assertion names it.
    """
    # LIFO: the two calls nest, so the nearest preceding tool_use is the right one.
    lifo = _store(conn, [
        ("tool_use", {"tool": "lookup_customer", "input": {"email": "a@b.co"}}, 10),
        ("tool_use", {"tool": "search_docs", "input": {"query": "refunds"}}, 105),
        # Resolves against the search_docs row at 105, not the lookup row at 10.
        ("tool_result", {"tool": "search_docs", "is_error": False,
                         "result": {"results": []}}, 110),
        ("tool_result", {"tool": "lookup_customer", "is_error": False,
                         "result": {"found": False}}, 210),
    ], run_uid="durations-lifo")
    # FIFO: the two calls overlap and complete in the order they started, so a global
    # stack pairs each result with the OTHER tool's use.
    fifo = _store(conn, [
        ("tool_use", {"tool": "lookup_customer", "input": {"email": "a@b.co"}}, 10),
        ("tool_use", {"tool": "search_docs", "input": {"query": "refunds"}}, 20),
        ("tool_result", {"tool": "lookup_customer", "is_error": False,
                         "result": {"found": False}}, 60),
        ("tool_result", {"tool": "search_docs", "is_error": False,
                         "result": {"results": []}}, 100),
    ], run_uid="durations-fifo")
    known = _known_tools(registry)

    steps = project_run_detail(lifo, full_fidelity=False, known_tools=known)

    assert [s["seq"] for s in steps] == [1, 2, 3, 4]
    assert [s["elapsed_ms"] for s in steps] == [10, 105, 110, 210]
    durations = {s["tool"]: s["duration_ms"] for s in steps if s["type"] == "tool_result"}
    assert durations == {"search_docs": 5, "lookup_customer": 200}

    overlapped = project_run_detail(fifo, full_fidelity=False, known_tools=known)
    assert {
        s["tool"]: s["duration_ms"] for s in overlapped if s["type"] == "tool_result"
    } == {"lookup_customer": 50, "search_docs": 80}

    # A tool_result with no tool_use to pair against — the first row of a run whose
    # earlier rows the retention swept — carries None rather than raising.
    orphan = _store(conn, [
        ("tool_result", {"tool": "send_reply", "is_error": False,
                         "result": {"reply_id": 1, "status": "resolved"}}, 90),
    ], run_uid="durations-orphan")
    assert project_run_detail(
        orphan, full_fidelity=False, known_tools=known
    )[0]["duration_ms"] is None

    # Second resolution, so it is misleading as a timing and it is not published.
    assert "created_at" not in json.dumps(steps, default=str)
    assert all("created_at" not in step for step in steps)


def test_cited_is_computed_against_the_accepted_reply(conn, registry):
    """A chunk is cited iff an id it LICENSES is in the accepted send_reply's citations.

    Each search_docs hit licenses its `doc`, its `id` and every one of its `anchors` —
    the same set agent.py adds to the run's accept-set — and a denied attempt's
    citations are not "cited": the guardrail row already tells that story.

    MUTATION that must turn this red: drop normalise_citation from the CITED side
    (`cited.update(c for c in citations ...)`) — the accepted reply's differently-cased
    citation matches nothing and the grounded chunk renders as not-cited.
    SECOND, INDEPENDENT MUTATION: drop it from the LICENSED side. That is what the
    third hit below exists for: `run_events` is a back catalogue, so a row written by
    an older build can hold an id this build would have lowercased, and only a hit
    whose own licensed ids are NOT already normalised can tell the two sides apart. I
    ran this test without that hit and the licensed-side mutation passed.
    THIRD MUTATION: count the DENIED attempt's citations too — api.md then renders as
    cited, an audit view claiming grounding the guard refused.
    """
    hit = {
        "doc": "billing.md", "heading": "Refunds", "id": "billing.md#refunds",
        "anchors": ["billing.md", "billing.md#refunds"], "text": "prose", "score": 0.9,
    }
    other = {
        "doc": "api.md", "heading": "Rate limits", "id": "api.md#rate-limits",
        "anchors": ["api.md", "api.md#rate-limits"], "text": "prose", "score": 0.4,
    }
    # A historical row whose licensed ids were never normalised on the way in.
    legacy = {
        "doc": "SSO.md", "heading": "SSO (Enterprise)", "id": "SSO.md#SSO-Enterprise",
        "anchors": ["SSO.md", "SSO.md#SSO-Enterprise"], "text": "prose", "score": 0.7,
    }
    rows = _store(conn, [
        ("tool_result", {"tool": "search_docs", "is_error": False,
                         "result": {"results": [hit, other, legacy]}}, 10),
        # A DENIED attempt citing api.md — its citations must not count.
        ("tool_use", {"tool": "send_reply", "input": {
            "ticket_id": 77, "body": "z" * 40, "citations": ["api.md#rate-limits"]}}, 20),
        ("tool_result", {"tool": "send_reply", "is_error": True,
                         "result": {"error": "nope", "denied_by": "citation"}}, 25),
        # The ACCEPTED attempt. It cites billing.md in a case the guard normalises
        # away, and the legacy doc in the case THIS build would have minted — so one
        # citation exercises the cited side of the comparison and the other the
        # licensed side.
        ("tool_use", {"tool": "send_reply", "input": {"ticket_id": 77, "body": "z" * 40,
            "citations": [" BILLING.md#Refunds ", "sso.md#sso-enterprise"]}}, 30),
        ("tool_result", {"tool": "send_reply", "is_error": False,
                         "result": {"reply_id": 1, "status": "resolved"}}, 40),
    ])

    steps = project_run_detail(
        rows, full_fidelity=False, known_tools=_known_tools(registry)
    )

    search = next(s for s in steps if s["type"] == "tool_result" and s["tool"] == "search_docs")
    assert [(r["id"], r["cited"]) for r in search["results"]] == [
        ("billing.md#refunds", True),          # cited side needed normalising
        ("api.md#rate-limits", False),         # cited only by the DENIED attempt
        ("SSO.md#SSO-Enterprise", True),       # licensed side needed normalising
    ]


# Two docs, one query, keyword mode: "refunds and rate limits" retrieves api.md AND
# billing.md, so a run can cite one and leave the other retrieved-but-not-cited. A
# single-hit query would make the "every other chunk is False" half vacuous.
TWO_DOC_QUERY = "refunds and rate limits"


def _run_events_from_disk(type_filter: str | None = None) -> list:
    """This run's committed rows, re-opened from the file the app wrote them to."""
    reopened = connect(settings.db_path)
    try:
        run_uid = reopened.execute("SELECT run_uid FROM runs").fetchone()["run_uid"]
        rows = reopened.execute(
            "SELECT seq, type, payload, elapsed_ms, created_at FROM run_events"
            " WHERE run_uid = ? ORDER BY seq",
            (run_uid,),
        ).fetchall()
    finally:
        reopened.close()
    return [r for r in rows if type_filter is None or r["type"] == type_filter]


def _licensed_ids(hit: dict) -> set[str]:
    """Every id one search_docs hit licenses — agent.py:400-405, restated in the test.

    Restated rather than imported: if the projector and the test called one helper,
    a change to that helper would move both and the agreement would be with itself.
    """
    return {hit["doc"], hit["id"], *hit["anchors"]}


def test_cited_vs_not_matches_the_citation_guards_accept_set(client, capture_frames,
                                                             monkeypatch):
    """The drill-down's grounding highlight and the guard's decision provably agree.

    Driven as a REAL run through the recorder rather than hand-built rows, so the
    payload shapes are the ones production actually writes, and the guard is the real
    guard: the reply cites a retrieved id in different case and whitespace, and the
    run must reach `resolution via send_reply` — i.e. the control ACCEPTED it. The
    drill-down then has to say the same thing about the same reply.

    MUTATION that must turn this red: compare raw strings instead of normalise_citation
    on the CITED side. The citation the guard accepted renders as not-cited — an audit
    view contradicting the control it audits, which is worse than no audit view because
    a reader cannot tell which of the two is lying.

    SECOND MUTATION: count the DENIED attempt's citations. The run below makes a first
    send_reply that cites a genuinely retrieved id (api.md) but names another ticket,
    so the binding guard refuses it — api.md then renders cited, claiming grounding for
    a reply that was never sent.

    STATED PLAINLY: this test does NOT pin normalise_citation on the LICENSED side.
    Real retrieval mints ids that are already lowercase, so a run cannot produce a
    licensed id that needs normalising; that half is pinned by the unit test above,
    whose legacy hit carries an id an older build could have written. I ran the
    licensed-side mutation against this test and it passed.

    The expected set is derived from the run's OWN search results rather than
    hardcoded, so a change in kb/ content cannot quietly make this vacuous, and the
    retrieved set is asserted non-empty (and multi-doc) before anything is claimed
    about cited-vs-not.
    """
    # A real VOYAGE_API_KEY sits in .env; pinned to None so retrieval runs in keyword
    # mode and this test is free and deterministic by construction.
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "refunds and my rate limits?")
    app.state.client = FakeClient([
        response([tool_use_block("search_docs", {"query": TWO_DOC_QUERY})]),
        # A DENIED attempt that cites a real retrieved id. The citation guard would
        # have accepted it; the ticket binding refuses the call for another reason —
        # which is the point: "cited" must mean "in a reply that was actually sent",
        # not "in something the model typed".
        response([tool_use_block("send_reply", {
            "ticket_id": 999_999,
            "body": "Your rate limit is per-minute — see the API doc.",
            "citations": ["api.md#rate-limits"],
        }, id="toolu_denied")]),
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id,
            "body": "Refunds take 14 days and your rate limit is per-minute.",
            # The same id the run retrieved, retyped by the model in another case with
            # stray whitespace. The guard accepts it; so must the drill-down.
            "citations": ["  BILLING.md#Refunds\n"],
        }, id="toolu_ok")]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    body, _frames = asyncio.run(capture_frames(ticket_id))

    # The CONTROL's verdicts, both of them: one send_reply refused, one accepted, and
    # the run resolved by replying. Without this the agreement below would be between
    # two failures.
    assert "event: error" not in body
    assert "event: resolution" in body
    rows = _run_events_from_disk()
    parsed = [(r, json.loads(r["payload"])) for r in rows]
    send_results = [
        d for r, d in parsed if r["type"] == "tool_result" and d["tool"] == "send_reply"
    ]
    assert [d["is_error"] for d in send_results] == [True, False], send_results
    assert send_results[0]["result"]["denied_by"] == "ticket_binding"

    search_payload = next(
        d for r, d in parsed if r["type"] == "tool_result" and d["tool"] == "search_docs"
    )
    hits = search_payload["result"]["results"]
    assert len(hits) >= 2, f"the query retrieved {len(hits)} doc(s) — nothing to contrast"

    # The guard's own accept-set, rebuilt from this run's results exactly as agent.py
    # builds it, and the citations the ACCEPTED reply carried. Derived by walking the
    # rows here rather than by calling the projector: an agreement computed by the
    # thing under test is an agreement with itself.
    accept_set = {normalise_citation(i) for hit in hits for i in _licensed_ids(hit)}
    sends = [
        d for r, d in parsed
        if r["type"] in ("tool_use", "tool_result") and d["tool"] == "send_reply"
    ]
    citations = next(
        use["input"]["citations"]
        for use, result in zip(sends[0::2], sends[1::2], strict=True)
        if not result["is_error"]
    )
    accepted_citations = {normalise_citation(c) for c in citations}
    assert accepted_citations <= accept_set, "the guard would not have accepted these"
    expected_cited = {
        hit["id"] for hit in hits
        if {normalise_citation(i) for i in _licensed_ids(hit)} & accepted_citations
    }
    assert expected_cited, "no retrieved chunk was cited — the assertion below is vacuous"
    assert expected_cited != {hit["id"] for hit in hits}, "every chunk was cited"

    steps = project_run_detail(
        rows,
        full_fidelity=False,
        known_tools=_known_tools(app.state.registry),
    )
    search_step = next(
        s for s in steps if s["type"] == "tool_result" and s["tool"] == "search_docs"
    )
    assert {r["id"] for r in search_step["results"] if r["cited"]} == expected_cited
    assert {r["id"] for r in search_step["results"] if not r["cited"]} == (
        {hit["id"] for hit in hits} - expected_cited
    )


def test_cited_is_false_when_no_reply_was_accepted(client, capture_frames, monkeypatch):
    """An escalating run marks every retrieved chunk not-cited, and does not raise.

    The empty-cited-set path is a legible state, not an error: a run can search and
    then escalate, and "retrieved, not cited" is exactly the true thing to render. A
    projector that treated the missing send_reply as an exceptional case would 500 the
    drill-down for every escalation in the back catalogue — which is the outcome the
    demo shows off most.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "refunds and my rate limits?")
    app.state.client = FakeClient([
        response([tool_use_block("search_docs", {"query": TWO_DOC_QUERY})]),
        response([tool_use_block("create_escalation", {
            # >= 20 chars: CreateEscalationInput.reason has a min_length, and a
            # validation failure here would end the run "ended_without_action" and
            # quietly turn this into a test about a broken run.
            "ticket_id": ticket_id,
            "reason": "needs a human to review the refund window",
            "priority": "high",
        })]),
        response([text_block("escalated")], stop_reason="end_turn"),
    ])

    body, _frames = asyncio.run(capture_frames(ticket_id))
    assert "event: error" not in body
    rows = _run_events_from_disk()

    steps = project_run_detail(
        rows, full_fidelity=False, known_tools=_known_tools(app.state.registry)
    )
    search_step = next(
        s for s in steps if s["type"] == "tool_result" and s["tool"] == "search_docs"
    )
    # Non-empty first: "nothing is cited" over an empty result list proves nothing.
    assert search_step["results"], "the run retrieved nothing"
    assert all(r["cited"] is False for r in search_step["results"])
    assert next(s for s in steps if s["type"] == "resolution")["via"] == "create_escalation"


# --- Wave 3: the drill-down route and the plumbing that makes it usable --------------

DEMO_HEADERS = {"X-API-Key": "test-demo-key"}


def _demo_ticket(client, email: str, body: str, subject: str = "API limits") -> int:
    """Create a ticket as the DEMO tier, so tickets.origin is 'demo'.

    The counterpart to _make_ticket above, which rides the client fixture's default
    OWNER header. D-02's full-fidelity exception is anchored on the CREATION tier, so
    which key posted /tickets is the whole of the difference these tests turn on.
    """
    created = client.post(
        "/tickets",
        json={"customer_email": email, "subject": subject, "body": body},
        headers=DEMO_HEADERS,
    )
    assert created.status_code == 201
    return created.json()["id"]


def _origin_of(ticket_id: int) -> str | None:
    row = app.state.conn.execute(
        "SELECT origin FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    assert row is not None, f"ticket {ticket_id} does not exist"
    return row["origin"]


def test_ticket_origin_is_the_creation_tier(client):
    """Which key created the ticket is recorded on the row, server-side (D-02).

    This is the ONLY signal that a ticket is visitor-authored, and every later
    full-fidelity decision reads it. Three rows, three tiers:

    MUTATION that must turn this red: leave `dependencies=[Depends(create_gate)]` on
    the decorator and hardcode `origin` to a literal in the INSERT — the demo/owner
    distinction collapses and one of the first two assertions fails whichever literal
    is chosen. The tier has to be RETURNED by the gate and taken as a parameter; a gate
    declared in `dependencies=[...]` throws its own return value away.

    SECOND, INDEPENDENT MUTATION (threat T-06-15, elevation of privilege): anchor the
    flag on the /PROCESS tier instead — set origin when the run starts, from the key
    that called /tickets/{id}/process. An owner-created ticket containing a real
    customer's email and body then becomes full fidelity the moment ANYONE runs it with
    the published demo key, which is a disclosure reachable by one curl. The owner
    assertion below is what catches it: this ticket is never processed at all, so a
    process-anchored flag cannot be 'owner'. `test_a_demo_originated_run_is_full_fidelity`
    and the leak test are the behavioural half of the same property.
    """
    owner_id = _make_ticket(client, "owner@acmecorp.com", "created with the owner key")
    demo_id = _demo_ticket(client, "visitor@example.com", "created with the demo key")

    assert _origin_of(owner_id) == "owner"
    assert _origin_of(demo_id) == "demo"

    # A row written by code that predates the column — i.e. what is on the Fly volume.
    # NULL, and NULL reads as NOT demo everywhere (fail-closed, 06-01). Asserted by
    # identity rather than falsiness: `origin == "demo"` is the check the route makes,
    # and a truthiness check here would not notice if the route used one too.
    cur = app.state.conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("legacy@acmecorp.com", "legacy", "written before origin existed"),
    )
    app.state.conn.commit()
    assert _origin_of(cur.lastrowid) is None


def test_create_gate_is_not_charged_twice(client, monkeypatch):
    """Moving the gate into the signature must not leave a second copy metering.

    With the demo create allowance at exactly one, the FIRST demo POST has to succeed.
    A gate declared BOTH in `dependencies=[...]` and as a parameter default runs twice
    per request, so the single request spends two units and 429s itself — the perimeter
    silently halving every demo limit in D-04.

    STATED PLAINLY: this is a REGRESSION GUARD, not proof of new behaviour. It passes
    against the pre-change code too (one declaration, charged once). Its whole value is
    that it fails the moment someone adds the parameter without deleting the decorator.
    """
    monkeypatch.setattr(settings, "demo_create_limit", "1/hour")
    first = client.post(
        "/tickets",
        json={
            "customer_email": "visitor@example.com",
            "subject": "one shot",
            "body": "the first demo create must be admitted",
        },
        headers=DEMO_HEADERS,
    )
    assert first.status_code == 201, (
        "the first demo POST /tickets was refused — the create gate was charged twice"
        f" for one request: {first.text}"
    )
    assert _origin_of(first.json()["id"]) == "demo"

    # And the allowance really was 1, so the assertion above was not vacuous.
    second = client.post(
        "/tickets",
        json={
            "customer_email": "visitor@example.com",
            "subject": "two shots",
            "body": "the second demo create must be refused",
        },
        headers=DEMO_HEADERS,
    )
    assert second.status_code == 429


def test_process_returns_the_run_uid_to_the_submitter(client, monkeypatch):
    """POST /process hands back X-Relay-Run-Uid — the run's own identity (Finding 2).

    Try-it streams /process with `fetch` and simultaneously watches the ambient
    /events feed, which carries the REDACTED projection of the same run. Without the
    uid the page renders one run twice, in two fidelities, with no way to connect
    them — and cannot deep-link its own drill-down.

    MUTATION that must turn this red: mint `run_uid = uuid.uuid4().hex` back inside
    `event_stream` (where it was) — the handler has no uid to put on the response and
    the header is absent. The uid must be minted in the HANDLER and closed over.

    The header, not a new SSE frame: the milestone's compatibility constraint keeps the
    event contract byte-unchanged, and `scripts/demo.sh` would print an extra frame.
    Same-origin `fetch` reads response headers with no CORS allowlist needed.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    app.state.client = FakeClient([
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id,
            "body": "Your Pro plan allows 600 requests/minute per workspace.",
        })]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    resp = client.post(f"/tickets/{ticket_id}/process")
    assert resp.status_code == 200
    assert "event: error" not in resp.text

    uid = resp.headers.get("X-Relay-Run-Uid")
    assert uid, "POST /process returned no X-Relay-Run-Uid header"
    assert re.fullmatch(r"[0-9a-f]{32}", uid), uid

    # The header is not merely present, it is THE run's key: the same value on both
    # sides of the soft join the drill-down reads. A uid minted for the header and a
    # second one minted for the rows would satisfy a presence check and nothing else.
    reopened = connect(settings.db_path)
    try:
        assert reopened.execute(
            "SELECT run_uid FROM runs WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()["run_uid"] == uid
        rows = reopened.execute(
            "SELECT seq FROM run_events WHERE run_uid = ?", (uid,)
        ).fetchall()
    finally:
        reopened.close()
    assert rows, "no run_events row carried the uid the header advertised"

    # And the SSE contract is untouched: no new event name appeared alongside it.
    streamed = {
        line[len("event: "):] for line in resp.text.splitlines()
        if line.startswith("event: ")
    }
    assert "run" not in streamed and "run_uid" not in streamed, streamed


def test_streaming_routes_are_not_buffered(client, monkeypatch):
    """Both SSE routes tell proxies and caches to let the bytes through (05-REVIEW IN-02).

    `X-Accel-Buffering: no` is nginx's opt-out and is honoured by the Fly proxy chain;
    without it a buffering hop collects the whole stream and delivers it at once, which
    is the difference between "the feed is live" and "the page hangs for 20 seconds and
    then dumps everything". `Cache-Control: no-cache` stops an intermediary serving a
    second visitor the first one's run.

    MUTATION that must turn this red: drop the `headers={...}` argument from either
    StreamingResponse.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    # So /events closes on its own instead of holding the TestClient for its 300s
    # ceiling — this test is about the response headers, not the stream.
    monkeypatch.setattr(settings, "events_heartbeat_seconds", 0.01)
    monkeypatch.setattr(settings, "events_idle_seconds", 0.02)

    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    app.state.client = FakeClient([
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id,
            "body": "Your Pro plan allows 600 requests/minute per workspace.",
        })]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])

    for resp in (
        client.post(f"/tickets/{ticket_id}/process"),
        client.get("/events"),
    ):
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers.get("cache-control") == "no-cache", resp.headers
        assert resp.headers.get("x-accel-buffering") == "no", resp.headers


@contextmanager
def _anon(client):
    """Drop the fixture's credential for one block.

    Restated here rather than imported from tests/test_auth.py: the client fixture puts
    X-API-Key on the client's DEFAULT headers, so a keyless request has to remove it and
    not merely omit it — and a "public route" test that quietly sent the owner key would
    be the vacuous form of exactly the claim it makes.
    """
    saved = client.headers.pop("X-API-Key")
    try:
        yield client
    finally:
        client.headers["X-API-Key"] = saved


def _drive_a_denied_then_accepted_run(client, ticket_id: int) -> str:
    """Script one run that produces tool_use, tool_result AND guardrail rows.

    The first send_reply names another ticket, so the binding guard denies it and the
    agent yields a `guardrail` event before the failed tool_result; the second is
    accepted and resolves the run. Returns the run's uid, taken from the response
    header — i.e. exactly what a Try-it visitor would hold.
    """
    app.state.client = FakeClient([
        response([tool_use_block("lookup_customer", {"email": "liam@brightco.io"})]),
        response([tool_use_block("send_reply", {
            "ticket_id": 999_999,
            "body": "Your rate limit is 600 requests/minute on the Pro plan.",
        }, id="toolu_denied")]),
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id,
            "body": "Your rate limit is 600 requests/minute on the Pro plan.",
        }, id="toolu_ok")]),
        response([text_block("replied")], stop_reason="end_turn"),
    ])
    resp = client.post(f"/tickets/{ticket_id}/process")
    assert resp.status_code == 200
    assert "event: error" not in resp.text
    assert "event: resolution" in resp.text
    return resp.headers["X-Relay-Run-Uid"]


def _insert_runs_row(uid: str, *, ticket_id: int, age_days: int = 0) -> None:
    """A `runs` row aged by SQLite's own clock — the swept/unrecorded split turns on it.

    `datetime('now', ?)` rather than a Python-formatted literal, so the row's timestamp
    is produced by exactly the expression the retention comparison uses.
    """
    app.state.conn.execute(
        "INSERT INTO runs (ticket_id, model, duration_ms, steps, input_tokens,"
        " output_tokens, cost_usd, outcome, created_at, run_uid)"
        " VALUES (?, 'claude-sonnet-5', 120, 3, 1000, 500, 0.02, 'send_reply',"
        " datetime('now', ?), ?)",
        (ticket_id, f"-{age_days} days", uid),
    )
    app.state.conn.commit()


def test_run_detail_returns_a_complete_run(client, monkeypatch):
    """DASH-03: a keyless GET /runs/{uid} renders one run's redacted steps.

    Driven as a REAL run through the recorder, so the rows are the shapes production
    writes, and fetched with NO API key — this route is public like /events and
    /metrics, and its safety is content control, not access control.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    uid = _drive_a_denied_then_accepted_run(client, ticket_id)

    with _anon(client) as anon:
        resp = anon.get(f"/runs/{uid}")
    assert resp.status_code == 200
    detail = resp.json()

    assert detail["run_uid"] == uid
    assert detail["ticket_id"] == ticket_id
    assert detail["status"] == "complete"
    # Owner-created, so redacted. The FLAG is not secret; the content is.
    assert detail["demo"] is False
    assert "ticket" not in detail, "a non-demo drill-down published the ticket text"

    run = detail["run"]
    assert run["outcome"] == "send_reply"
    assert run["ticket_id"] == ticket_id
    assert run["steps"] >= 1 and run["duration_ms"] >= 0
    assert run["cost_usd"] > 0
    assert set(run) == {
        "outcome", "cost_usd", "duration_ms", "steps", "input_tokens",
        "output_tokens", "model", "created_at", "ticket_id",
    }, run

    steps = detail["steps"]
    assert steps, "a complete run rendered no steps at all"
    # Non-empty is not enough: a drill-down that publishes only `usage` frames would
    # satisfy it and show nothing of what the agent DID. All three of the types this
    # page exists to render have to be there.
    assert {"tool_use", "tool_result", "guardrail"} <= {s["type"] for s in steps}
    assert [s["seq"] for s in steps] == sorted(s["seq"] for s in steps)
    guard = next(s for s in steps if s["type"] == "guardrail")
    assert guard["guard"] == "ticket_binding" and guard["action"] == "denied"


def test_run_detail_404s_on_a_malformed_or_unknown_uid(client, monkeypatch):
    """A uid that cannot exist is refused before the database is touched (T-06-17).

    The uid shape is known exactly (uuid4().hex), so a scripted walk of made-up keys
    must cost a regex and not a query — the route is public, keyless, and over a table
    that grows with the whole back catalogue.

    MUTATION that must turn this red: drop the `_RUN_UID_RE.match` guard — the DB
    sentinel below then fires, because every malformed uid reaches a SELECT.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)

    real_conn = app.state.conn

    class _ExplodingConn:
        """Any DB touch at all is the failure this test is about."""

        def __getattr__(self, name):
            raise AssertionError(f"a malformed uid reached the database (conn.{name})")

    malformed = [
        "abc",
        "Z" * 32,                 # 32 chars, not hex
        "0" * 31,                 # right alphabet, wrong length
        "0123456789ABCDEF" * 2,   # uppercase hex — uuid4().hex is lowercase
        "' OR 1=1 --",
        "0" * 32 + "; DROP TABLE runs",
    ]
    app.state.conn = _ExplodingConn()
    try:
        with _anon(client) as anon:
            for uid in malformed:
                resp = anon.get(f"/runs/{uid}")
                assert resp.status_code == 404, (uid, resp.status_code)
                # The short-string domain form, not the perimeter's dict — and it names
                # no uid back, so the 404 body cannot be used as an echo oracle.
                assert resp.json()["detail"] == "unknown run", (uid, resp.json())
            # Traversal never even reaches this route: httpx (like curl) removes dot
            # segments client-side, and a %2f-encoded separator is decoded before
            # routing, so neither matches the single-segment /runs/{run_uid}. Still a
            # 404, one hop earlier — asserted on the status alone, and still with the
            # exploding connection installed, so the DB is untouched on this path too.
            assert anon.get("/runs/../../etc/passwd").status_code == 404
            assert anon.get("/runs/%2e%2e%2f%2e%2e%2fetc%2fpasswd").status_code == 404
    finally:
        app.state.conn = real_conn

    # Well-formed but absent: this one MAY touch the database, and still 404s.
    with _anon(client) as anon:
        absent = anon.get("/runs/" + "0" * 32)
    assert absent.status_code == 404
    assert absent.json()["detail"] == "unknown run"


def test_run_detail_of_a_swept_run_renders_as_swept(client):
    """The four absence states are distinguishable, and only one of them is a 404.

    `purge_expired_run_events` deletes run_events at 30 days and deliberately spares the
    `runs` row, so "no steps" is the NORMAL end state of every run in the back
    catalogue. A visitor following a link into it must be told the steps expired.

    MUTATION that must turn this red: return 404 when the event rows are missing. A
    30-day-old run then becomes indistinguishable from a forged uid — the page can no
    longer tell "this run happened and its detail expired" from "this uid is a lie",
    and the honest retention story silently reads as a broken link (T-06-18).
    """
    swept_uid, unrecorded_uid, in_flight_uid = ("a" * 32, "b" * 32, "c" * 32)
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")

    # (1) runs row present, events swept by retention.
    _insert_runs_row(
        swept_uid, ticket_id=ticket_id, age_days=settings.events_retention_days + 5
    )
    # (2) runs row present, INSIDE the window: a legacy pre-Phase-5 run, or a run whose
    # per-step writes failed. Not swept — nothing expired — and saying "swept" about it
    # would be a lie about retention.
    _insert_runs_row(unrecorded_uid, ticket_id=ticket_id, age_days=0)
    # (3) events present, runs row not yet written — the row lands in event_stream's
    # finally, so this is what EVERY run looks like while it is streaming.
    app.state.conn.execute(
        "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload, elapsed_ms)"
        " VALUES (?, ?, 1, 'usage', ?, 12)",
        (in_flight_uid, ticket_id, json.dumps({
            "steps": 1, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001,
        })),
    )
    app.state.conn.commit()

    with _anon(client) as anon:
        swept = anon.get(f"/runs/{swept_uid}")
        unrecorded = anon.get(f"/runs/{unrecorded_uid}")
        in_flight = anon.get(f"/runs/{in_flight_uid}")
        absent = anon.get("/runs/" + "d" * 32)

    assert swept.status_code == 200
    assert swept.json()["status"] == "swept"
    assert swept.json()["steps"] == []
    # A note the page can render verbatim, naming the window rather than a bare state.
    assert str(settings.events_retention_days) in swept.json()["note"]
    assert swept.json()["run"]["outcome"] == "send_reply"

    assert unrecorded.status_code == 200
    assert unrecorded.json()["status"] == "unrecorded"
    assert unrecorded.json()["steps"] == []
    assert "note" not in unrecorded.json()

    assert in_flight.status_code == 200
    assert in_flight.json()["status"] == "in_flight"
    assert in_flight.json()["run"] is None
    assert [s["type"] for s in in_flight.json()["steps"]] == ["usage"]
    assert in_flight.json()["ticket_id"] == ticket_id

    # The ONLY 404: nothing under this uid on either side.
    assert absent.status_code == 404


def test_run_detail_is_rate_limited_per_ip(client, monkeypatch):
    """T-06-17: a scripted walk of the back catalogue is metered, in its own bucket.

    MUTATION that must turn this red: drop `dependencies=[Depends(run_detail_gate)]`
    from the route decorator — the second GET returns 404/200 instead of 429 and the
    endpoint is back to unlimited free reads over every run the volume holds.

    Its OWN bucket, not /events': a drill-down flood must not spend the live feed's
    reconnect allowance (which would break the visitor's own feed) or hide itself
    inside the feed's log line.
    """
    monkeypatch.setattr(settings, "anon_run_detail_limit", "1/minute")
    uid = "e" * 32
    with _anon(client) as anon:
        first = anon.get(f"/runs/{uid}")
        second = anon.get(f"/runs/{uid}")

    # Metered before the handler, so even a 404 spends its unit — an unknown-uid walk
    # is exactly the traffic this bounds.
    assert first.status_code == 404
    assert second.status_code == 429
    assert second.json()["detail"]["error"] == "rate_limited"
    assert second.headers["Retry-After"]


def test_run_detail_read_is_bounded(client, monkeypatch):
    """The per-run read carries a LIMIT — one run cannot be an unbounded response.

    run_events grows by ~10 rows per run and nothing caps a single run's step count
    (the agent's step limit is the only bound, and it is configuration). A route that
    materialises every row of the largest run in the catalogue, on the loop that
    answers the 3s container HEALTHCHECK, is a denial-of-service surface with a public
    URL.

    MUTATION that must turn this red: drop `LIMIT ?` from the run_events SELECT.
    """
    monkeypatch.setattr(settings, "run_detail_max_events", 2)
    uid = "f" * 32
    ticket_id = _make_ticket(client, "liam@brightco.io", "What are my rate limits?")
    for seq in range(1, 6):
        app.state.conn.execute(
            "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload, elapsed_ms)"
            " VALUES (?, ?, ?, 'usage', ?, ?)",
            (uid, ticket_id, seq, json.dumps({
                "steps": seq, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001,
            }), seq * 10),
        )
    app.state.conn.commit()

    with _anon(client) as anon:
        detail = anon.get(f"/runs/{uid}").json()

    # Exactly the limit, and the FIRST rows by seq — a truncated run must read as its
    # beginning, not as an arbitrary window.
    assert [s["seq"] for s in detail["steps"]] == [1, 2]


def test_budget_gauge_matches_the_gate(client):
    """D-11: /metrics renders the number that refuses the visitor's next run.

    MUTATION that must turn this red: compute the gauge's spend from the response's own
    `last_runs` rows instead of calling budget_snapshot. The reservation held below
    vanishes from the gauge, so the page advertises budget remaining while /process is
    already 503-ing — a credibility failure on the one page whose whole purpose is
    credibility.
    """
    record_run(app.state.conn, ticket_id=1, model="m", duration_ms=10, steps=1,
               input_tokens=1, output_tokens=1, cost_usd=0.25, outcome="send_reply")

    # A run admitted but not yet written to `runs`. The GATE counts this; a gauge
    # derived from committed rows alone cannot see it, which is the whole mutation.
    token = reserve_run()
    try:
        payload = client.get("/metrics").json()
        expected = budget_snapshot(app.state.conn)
    finally:
        release_run(token)

    assert payload["budget"] == expected
    assert set(payload["budget"]) == {
        "spent_today_usd", "daily_ceiling_usd", "remaining_usd", "exhausted", "resets_at",
    }
    # And the reservation really was included, so the equality above was not vacuous:
    # the gauge reads strictly above the committed rows it could have summed instead.
    committed = sum(r["cost_usd"] for r in payload["last_runs"])
    assert payload["budget"]["spent_today_usd"] > committed >= 0.25, payload["budget"]
    assert payload["budget"]["exhausted"] is False


def test_a_legacy_null_origin_run_is_redacted_at_the_route(client, monkeypatch):
    """origin IS NULL fails closed AT THE ROUTE, not merely at the column (D-02).

    The Fly volume is full of rows written before `origin` existed. 06-01 pinned the
    stored value as NULL; this pins what the PUBLIC ROUTE does with it, which is the
    half a visitor can actually reach. The ticket is inserted by raw SQL — exactly as a
    pre-migration deploy wrote it — and then driven through a real recorded run.

    MUTATION that must turn this red: derive full fidelity by truthiness or by
    `origin != "owner"` instead of `origin == "demo"`. NULL is neither 'demo' nor
    'owner', so a fail-open comparison publishes a legacy customer's raw ticket body
    and tool inputs to anyone holding the uid.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    secret = "LEGACY-NULL-ORIGIN-SENTINEL-3ad91f"
    cur = app.state.conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        ("legacy@acmecorp.com", "written before origin existed", f"help: {secret}"),
    )
    app.state.conn.commit()
    ticket_id = cur.lastrowid
    assert _origin_of(ticket_id) is None, "the legacy row was classified after all"

    app.state.client = FakeClient([
        response([tool_use_block("create_escalation", {
            "ticket_id": ticket_id,
            "reason": f"customer reported: {secret} — needs a human review",
            "priority": "high",
        })]),
        response([text_block("escalated")], stop_reason="end_turn"),
    ])
    resp = client.post(f"/tickets/{ticket_id}/process")
    assert resp.status_code == 200
    assert "event: error" not in resp.text
    uid = resp.headers["X-Relay-Run-Uid"]

    # Present in the raw rows first — otherwise the absence below is vacuous.
    raw = "".join(
        r["payload"] for r in
        app.state.conn.execute(
            "SELECT payload FROM run_events WHERE run_uid = ?", (uid,)
        ).fetchall()
    )
    assert secret in raw, "the sentinel never reached the run's raw rows"

    with _anon(client) as anon:
        detail = anon.get(f"/runs/{uid}").json()
    assert detail["demo"] is False
    assert detail["steps"], "the drill-down published nothing — a vacuous pass"
    assert secret not in json.dumps(detail), "a NULL-origin run rendered full fidelity"
    assert "ticket" not in detail


# --- Wave 3: the load-bearing leak test ---------------------------------------------
#
# Four sentinels, each riding a DIFFERENT field the raw run_events payload actually
# carries, so no single redaction closes all four and a partial fix cannot pass. Distinct
# and improbable on purpose: a substring search for "email" would match half the corpus,
# and a sentinel that could occur by accident makes an absence assertion mean nothing.
DRILL_EMAIL = "drill-sentinel-4e81c7@example.com"     # -> lookup_customer input + result
DRILL_BODY = "SENTINEL-DRILL-BODY-92fa10"             # -> create_escalation.reason
DRILL_KEY = "sk-ant-SENTINEL-DRILL-KEY-6d0b3e"        # -> search_docs.query
DRILL_CITE = "sentinel-fabricated-doc-7b4c92.md"      # -> guardrail.missing_citations

DRILL_SENTINELS = (
    ("customer email", DRILL_EMAIL),
    ("ticket body", DRILL_BODY),
    ("api key", DRILL_KEY),
    ("fabricated citation", DRILL_CITE),
)


def _script_the_four_vector_run(ticket_id: int) -> None:
    """One run that carries all four sentinels into four different payload fields.

    Every vector is an OBSERVED field rather than model prose: `text` events are the
    one thing the public branch reduces to a character count, so routing a secret only
    through prose would make three quarters of this vacuous (Pitfall 4).
    """
    app.state.client = FakeClient([
        # (1) email -> tool_use.input.email, and -> tool_result.result.customer.email
        response([tool_use_block("lookup_customer", {"email": DRILL_EMAIL})]),
        # (2) fake key -> tool_use.input.query
        response([tool_use_block("search_docs", {"query": f"rotating {DRILL_KEY}"})]),
        # (3) a citation naming a doc the run never retrieved -> the citation guard
        # denies the reply and its guardrail event carries missing_citations.
        response([tool_use_block("send_reply", {
            "ticket_id": ticket_id,
            "body": "Rotate the key from the dashboard; the old one stops working.",
            "citations": [DRILL_CITE],
        })]),
        # (4) ticket body -> create_escalation.reason, an OBSERVED field, plus the same
        # string in model prose so the text branch is exercised on the way past.
        response([
            text_block(f"The customer wrote: {DRILL_BODY}"),
            tool_use_block("create_escalation", {
                "ticket_id": ticket_id,
                "reason": f"customer reported: {DRILL_BODY} — needs a human review",
                "priority": "high",
            }),
        ]),
        response([text_block("escalated")], stop_reason="end_turn"),
    ])


def _seed_the_looked_up_customer() -> None:
    """A real customers row, so lookup_customer returns a whole record — email, name,
    plan — and the drill-down has something genuine it has to drop."""
    app.state.conn.execute(
        "INSERT INTO customers (email, name, plan, signed_up) VALUES (?, ?, ?, ?)",
        (DRILL_EMAIL, "Drill Sentinel", "enterprise", "2025-01-01"),
    )
    app.state.conn.commit()


def _prove_the_sentinels_are_really_in_the_run(uid: str, body: str) -> None:
    """Presence, twice, before any absence is claimed.

    Without this half the whole test would stay green against a run that never carried
    the secrets — the unfalsifiable form of exactly this check, and the degenerate pass
    this project has shipped before.
    """
    assert "event: error" not in body
    for name, sentinel in DRILL_SENTINELS:
        assert sentinel in body, (
            f"the {name} sentinel never reached the run's own owner-facing stream —"
            " every absence assertion below would be vacuous"
        )
    raw = "".join(
        r["payload"] for r in app.state.conn.execute(
            "SELECT payload FROM run_events WHERE run_uid = ?", (uid,)
        ).fetchall()
    )
    for name, sentinel in DRILL_SENTINELS:
        # D-01: run_events is private and full-fidelity. If redaction had leaked into
        # the PERSISTENCE path, the drill-down would be clean for the wrong reason and
        # this phase would have nothing to drill into.
        assert sentinel in raw, (
            f"the {name} sentinel is missing from the raw run_events rows"
        )


def test_run_detail_never_leaks_a_non_demo_runs_content(client, monkeypatch):
    """THE load-bearing test (T-06-13): a non-demo run's public drill-down discloses
    none of the run's seeded secrets.

    /runs/{uid} is keyless and reachable with any uid harvested from the public feed or
    /metrics, over a 30-day back catalogue of other people's customer emails, ticket
    bodies and reply text. This is the phase's security boundary, and it is the only
    place where the raw store becomes reachable from the internet.

    MUTATION that must turn this red for ALL FOUR sentinels: forward the raw payload on
    project_run_detail's public branch — e.g. `step.update(payload)` in the tool_use
    branch, or returning `{**step, **payload}` at the end of the loop. Every sentinel
    rides a payload field, so one spread opens all four.

    SECOND, INDEPENDENT MUTATION: pass `full_fidelity=True` unconditionally from the
    route (or give the projector a `full_fidelity: bool = True` default and drop the
    keyword at the call site). The redaction code is untouched and correct; the
    AUTHORISATION is what fails, which is the failure this route can actually have.

    Presence is proved TWICE first — in the run's own owner-facing stream and in the
    raw run_events rows — and the anti-vacuity assertions at the end are what stop a
    drill-down that leaks nothing because it publishes nothing.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    _seed_the_looked_up_customer()
    # The OWNER key, so tickets.origin is 'owner' and the run is redacted.
    ticket_id = _make_ticket(
        client, DRILL_EMAIL, f"my key {DRILL_KEY} stopped working. {DRILL_BODY}"
    )
    _script_the_four_vector_run(ticket_id)

    resp = client.post(f"/tickets/{ticket_id}/process")
    assert resp.status_code == 200
    uid = resp.headers["X-Relay-Run-Uid"]
    _prove_the_sentinels_are_really_in_the_run(uid, resp.text)
    assert _origin_of(ticket_id) == "owner"

    with _anon(client) as anon:
        detail = anon.get(f"/runs/{uid}").json()

    assert detail["demo"] is False
    steps = detail["steps"]

    # Per STEP and per SENTINEL, not against one concatenated blob: a single leaking
    # step is enough to fail this, the message names which step and which secret, and
    # the useful answer under the mutations above is ALL the vectors that opened —
    # a leak that closes one field and leaves three is not a fix.
    leaks = [
        (i, step.get("type"), step.get("tool"), name)
        for i, step in enumerate(steps)
        for name, sentinel in DRILL_SENTINELS
        if sentinel in json.dumps(step)
    ]
    assert leaks == [], f"the drill-down's steps leaked seeded secrets: {leaks}"

    # And nowhere else in the response either — the envelope carries the run summary and
    # (on the demo branch only) ticket text, and a leak there is the same leak.
    whole = json.dumps(detail)
    envelope_leaks = [name for name, sentinel in DRILL_SENTINELS if sentinel in whole]
    assert envelope_leaks == [], f"the drill-down envelope leaked: {envelope_leaks}"
    assert "customer_email" not in whole

    # Anti-vacuity: a drill-down that published nothing would satisfy every assertion
    # above. It has to have rendered the run.
    assert steps, "the drill-down published no steps at all"
    assert {"tool_use", "tool_result", "guardrail"} <= {s["type"] for s in steps}
    tools = {s.get("tool") for s in steps if s["type"] == "tool_use"}
    assert {"lookup_customer", "search_docs", "create_escalation"} <= tools, tools
    # The redacted shape is genuinely there: the tool NAMES and argument KEY names are
    # published, so this is redaction rather than omission.
    lookup = next(s for s in steps if s["type"] == "tool_use" and s["tool"] == "lookup_customer")
    assert lookup["arg_keys"] == ["email"] and "input" not in lookup
    guard = next(s for s in steps if s["type"] == "guardrail")
    assert guard["guard"] == "citation" and guard["missing_count"] == 1
    assert "missing_citations" not in guard


def test_full_fidelity_is_server_decided(client, monkeypatch):
    """T-06-14: no query parameter, header or cookie can widen the disclosure.

    The full-fidelity flag is the one authorisation decision this phase makes, so every
    client-reachable input is a tampering vector. The route's signature takes exactly
    one path parameter; the flag is derived from tickets.origin and nothing else.

    MUTATION that must turn this red: accept the flag from the request — e.g.
    `async def run_detail(run_uid: str, full: bool = False)` and pass
    `full_fidelity=(full or origin == "demo")`. The byte-comparison below then differs
    and the sentinel assertion fires.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    _seed_the_looked_up_customer()
    ticket_id = _make_ticket(
        client, DRILL_EMAIL, f"my key {DRILL_KEY} stopped working. {DRILL_BODY}"
    )
    _script_the_four_vector_run(ticket_id)
    resp = client.post(f"/tickets/{ticket_id}/process")
    uid = resp.headers["X-Relay-Run-Uid"]
    _prove_the_sentinels_are_really_in_the_run(uid, resp.text)

    with _anon(client) as anon:
        plain = anon.get(f"/runs/{uid}")
        tampered = anon.get(
            f"/runs/{uid}",
            params={"full": "1", "fidelity": "raw", "demo": "true", "origin": "demo"},
            headers={
                "X-Demo": "1",
                "X-Relay-Origin": "demo",
                "X-Full-Fidelity": "true",
                "Cookie": "origin=demo; full=1",
            },
        )

    assert plain.status_code == tampered.status_code == 200
    # Byte-identical, not merely equivalent: a widened response that happened to
    # serialise to the same keys in a different order would still be a disclosure.
    assert tampered.content == plain.content
    for name, sentinel in DRILL_SENTINELS:
        assert sentinel not in tampered.text, f"tampering disclosed the {name} sentinel"


def test_a_demo_originated_run_is_full_fidelity(client, monkeypatch):
    """D-02's INVERSE: the Try-it visitor gets the raw trace of their OWN run.

    Without this, "full fidelity for demo runs" is untested and can silently regress to
    redacted-for-everyone — the drill-down would still pass every leak test above, and
    the whole payoff of the Try-it flow would be gone with nothing noticing.

    The visitor authored this content, so the raw tool inputs and outputs are the point.
    `customer_email` is still withheld (Q3): /tickets accepts an arbitrary address from
    anyone holding the published key, so it is the one field a visitor could use to
    publish a third party's identifier. The ticket's own address below is a separate
    sentinel that no tool ever sees, so its absence is a claim about the ENVELOPE and
    not an accident of the run's script.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    _seed_the_looked_up_customer()
    ticket_address = "demo-ticket-address-1c9e55@example.com"
    # The DEMO key, so tickets.origin is 'demo' — the whole difference from the leak
    # test above is which credential posted /tickets.
    ticket_id = _demo_ticket(
        client, ticket_address, f"my key {DRILL_KEY} stopped working. {DRILL_BODY}"
    )
    assert _origin_of(ticket_id) == "demo"
    _script_the_four_vector_run(ticket_id)
    resp = client.post(f"/tickets/{ticket_id}/process")
    uid = resp.headers["X-Relay-Run-Uid"]
    _prove_the_sentinels_are_really_in_the_run(uid, resp.text)

    with _anon(client) as anon:
        detail = anon.get(f"/runs/{uid}").json()

    assert detail["demo"] is True
    steps = detail["steps"]
    lookup = next(s for s in steps if s["type"] == "tool_use" and s["tool"] == "lookup_customer")
    # The raw input DICT, not just its key names — asserted as the value it carried.
    assert lookup["input"] == {"email": DRILL_EMAIL}
    lookup_result = next(
        s for s in steps if s["type"] == "tool_result" and s["tool"] == "lookup_customer"
    )
    assert lookup_result["result"]["customer"]["email"] == DRILL_EMAIL
    search = next(s for s in steps if s["type"] == "tool_use" and s["tool"] == "search_docs")
    assert DRILL_KEY in search["input"]["query"]
    escalation = next(
        s for s in steps if s["type"] == "tool_use" and s["tool"] == "create_escalation"
    )
    assert DRILL_BODY in escalation["input"]["reason"]
    guard = next(s for s in steps if s["type"] == "guardrail")
    assert guard["missing_citations"] == [DRILL_CITE]
    # Model prose is returned too, not just its length.
    assert any(DRILL_BODY in (s.get("text") or "") for s in steps if s["type"] == "text")

    # The visitor's own words back — named fields, not a spread of the ticket row.
    assert detail["ticket"] == {
        "subject": "API limits",
        "body": f"my key {DRILL_KEY} stopped working. {DRILL_BODY}",
    }
    # ...and the email is withheld even here, by key AND by value.
    assert "customer_email" not in json.dumps(detail)
    assert ticket_address not in json.dumps(detail)


# --- Wave 4: the packaged template (D-04) --------------------------------------------
#
# NOTE ON STRENGTH, stated once for every grep-level assertion in this wave: this suite
# has no DOM — no jsdom, no headless browser — so nothing below executes the dashboard's
# JavaScript. Assertions over the served HTML are regression guards on what was shipped,
# not evidence that a browser renders anything. Whether the cards, the charts and the
# gauge actually appear is a human check, and 06-07's checkpoint is where it closes.
# The two tests immediately below are the exception: they are genuine integration
# assertions over the response body and the installed package's own file.


def _packaged_template() -> Path:
    """The template as the INSTALLED package sees it.

    Resolved from `relay.__file__` and never from the repo root: a path like
    `Path(__file__).parent.parent / "src" / ...` passes in this checkout and says
    nothing about the wheel, which is the artifact the container actually runs.
    """
    import relay

    return Path(relay.__file__).parent / "templates" / "dashboard.html"


def test_dashboard_is_served_from_the_packaged_template(client):
    """The page comes out of a file that ships inside the package (D-04).

    MUTATION that must turn this red: point `_TEMPLATE_PATH` at a repo-root path
    (`Path(__file__).parent.parent.parent / "src" / "relay" / "templates" / ...`), or
    delete the template — the served body stops matching the packaged file's text.
    Hatchling honours .gitignore, so a future ignore rule can drop this file from the
    wheel with no build error; this test sees the path, and the CI docker smoke sees
    the artifact.
    """
    path = _packaged_template()
    assert path.is_file(), f"the template is not inside the installed package: {path}"

    raw = path.read_text(encoding="utf-8")
    assert "__RELAY_DEMO_KEY__" in raw, "the placeholder is gone from the file on disk"

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.text == raw.replace("__RELAY_DEMO_KEY__", "test-demo-key")


def test_dashboard_substitutes_the_key_per_request(client, monkeypatch):
    """The read is at import; the substitution is per request.

    MUTATION that must turn this red: bake the key at import
    (`DASHBOARD_HTML = _TEMPLATE_PATH.read_text().replace("__RELAY_DEMO_KEY__", ...)`)
    and return it unmodified — the second request below serves the first request's key.
    That is not a test-only failure: it is what ships a stale published key after a
    `fly secrets set`, on the one page whose job is to hand out a working key.
    """
    first = client.get("/dashboard").text
    assert "test-demo-key" in first

    monkeypatch.setattr(settings, "demo_key", "rotated-key-91c4de")
    second = client.get("/dashboard").text
    assert "rotated-key-91c4de" in second
    assert "test-demo-key" not in second

    # ...and the file on disk was never rewritten to do it.
    assert "__RELAY_DEMO_KEY__" in _packaged_template().read_text(encoding="utf-8")


# --- Wave 4: the page shell (DASH-02) ------------------------------------------------

_MARKUP_SINKS = ("inner" + "HTML", "outer" + "HTML", "insertAdjacent" + "HTML",
                 "document." + "write", "eval" + "(")


def _block(html: str, name: str) -> str:
    """One marker-delimited block of the page, isolated from everything around it.

    The marker assertion comes first for the reason tests/test_run_events.py:2068
    gives: with the markers gone, `split` would hand back the whole page (or blow up),
    and every assertion below would be vacuous or accidentally green off a comment
    somewhere else in the file.
    """
    begin, end = f"// --- {name} — begin ---", f"// --- {name} — end ---"
    assert begin in html and end in html, (
        f"the {name!r} block markers are gone — every assertion below would be vacuous"
    )
    return html.split(begin, 1)[1].split(end, 1)[0]


def _code_only(block: str) -> str:
    """A JS block with its `//` comment lines dropped.

    For assertions about what the code does NOT do: the comments in this file name the
    forbidden constructs on purpose (that is how the next reader learns why they are
    forbidden), so an absence assertion over the raw block would be about prose.
    """
    return "\n".join(
        line for line in block.splitlines() if not line.strip().startswith("//")
    )


def test_dashboard_never_renders_through_a_markup_sink(client):
    """T-06-19: one rendering rule, the WHOLE page — not just the feed block.

    Phase 5 scoped this rule to the live-feed block, because the tool name was the only
    model-chosen string on the page. The drill-down (06-06) and Try-it (06-07) render
    tool names, argument keys and the visitor's own text, and a block-scoped rule does
    not cover a block nobody has written yet — so the assertion is over the served
    document.

    MUTATION that must turn this red: render one card as markup
    (`host.innerHTML = "<div class=card>..."`) inside the metrics-poll block.

    WEAK BY CONSTRUCTION: there is no DOM in this suite. This greps the served HTML;
    it is a regression guard on what shipped, not evidence a browser renders anything
    safely. The real check is 06-07's human checkpoint.
    """
    html = client.get("/dashboard").text
    for sink in _MARKUP_SINKS:
        assert sink not in html, f"{sink} reached the dashboard — T-06-19"
    assert "textContent" in html, "nothing on the page writes a value as text at all"
    assert "createElement" in html


def test_dashboard_renders_the_summary_from_metrics(client):
    """DASH-02: the cards and the outcome bars are fed by /metrics' SQL-computed values.

    Asserted against the metrics-poll BLOCK, not the page at large: every bucket name
    also appears in telemetry's SQL and could be mentioned in a comment elsewhere, and
    an assertion that a name appears *somewhere* on a 250-line page proves nothing
    about what the poll reads.

    MUTATION that must turn this red: drop a bucket from OUTCOME_BUCKETS (the chart
    silently stops having a bar for it), or read the counts from `m.outcomes` — the raw
    outcome strings, which are NOT zero-filled and are not the seven display buckets.

    WEAK BY CONSTRUCTION: grep-level, as above.
    """
    html = client.get("/dashboard").text
    block = _block(html, "metrics poll (/metrics)")

    assert 'fetch("/metrics")' in block
    assert "outcome_distribution" in block
    buckets = block.split("const OUTCOME_BUCKETS = [", 1)[1].split("]", 1)[0]
    for bucket in ("resolved", "escalated", "dry_run", "incomplete",
                   "budget_exceeded", "step_limit", "error"):
        assert f'"{bucket}"' in buckets, f"the page never displays the {bucket} bucket"
    # Each card's source key, named against the block that builds the cards.
    for key in ("m.runs", "m.latency_ms", "m.cost_usd", "m.tokens", "m.last_runs"):
        assert key in block, f"the summary never reads {key}"


def test_dashboard_renders_without_a_demo_key(client, monkeypatch):
    """Pitfall 14: the public landing surface must survive an unconfigured deployment.

    The "None" assertion is tests/test_auth.py's, restated here deliberately: this plan
    rewrote the whole page, and a JS comment, an empty-state label or a `=== None` typo
    is exactly how that check goes red. Case-sensitive — CSS `display:none` is fine.

    MUTATION that must turn this red: label an empty state "None yet" instead of
    "no runs yet".
    """
    monkeypatch.setattr(settings, "demo_key", None)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "(not configured)" in resp.text
    assert "None" not in resp.text


# --- Wave 4: the charts and the budget gauge (DASH-04) -------------------------------


def test_charts_are_built_as_inline_svg_without_a_library(client):
    """DASH-04 / T-06-20: hand-built SVG, and no third-party script on the page.

    MUTATION that must turn this red: add a CDN chart library
    (`<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>`) and draw with it
    — the charts block loses createElementNS and the page gains a remote script, which
    is a supply chain on the public landing surface.

    WEAK BY CONSTRUCTION: grep over served HTML; nothing here executes the drawing.
    """
    html = client.get("/dashboard").text
    block = _block(html, "charts (SVG)")

    assert "createElementNS" in block
    assert 'SVGNS = "http://www.w3.org/2000/svg"' in block
    # Page-wide, not block-scoped: a remote script anywhere on the document is the
    # thing DASH-04 forbids, and it would not be added inside the charts block.
    for forbidden in ("<script src=", "cdn.", "unpkg", "import(", "importScripts"):
        assert forbidden not in html, f"{forbidden} reached the dashboard"
    assert not re.search(r"https://\S+\.(js|css)", html)


def test_the_gauge_reads_the_servers_budget_object(client):
    """D-11: the gauge renders the gate's arithmetic, never a second derivation.

    MUTATION that must turn this red: compute the fraction from last_runs
    (`m.last_runs.reduce((a, r) => a + r.cost_usd, 0) / ceiling`) — the spend RESERVED
    by runs in flight vanishes, the gauge reads up to concurrency x max_run_cost_usd
    low, and the page promises budget the next /process answers 503 to.

    WEAK BY CONSTRUCTION, and worth naming precisely: this greps the block for the
    server's keys and for the absence of a client-side sum. It cannot prove the two
    numbers agree — that proof is server-side, in
    test_budget_gauge_matches_what_the_gate_refuses_on (plan 06-04), which is where the
    single arithmetic is actually pinned.
    """
    html = client.get("/dashboard").text
    gauge = _block(html, "budget gauge")

    assert "spent_today_usd" in gauge and "daily_ceiling_usd" in gauge
    assert "remaining_usd" in gauge and "resets_at" in gauge
    # No re-derivation inside the gauge: no reduction, no accumulation, and no reading
    # of the per-run list at all — asserted over the CODE, with the `//` lines dropped,
    # because the comment explaining why the sum is forbidden names it too.
    code = _code_only(gauge)
    assert "last_runs" not in code
    assert "reduce(" not in code
    assert not re.search(r"\+=\s*\w+\.cost_usd", code)
    # The copy has to explain the jump, or the in-flight reservation reads as a bug.
    assert "in flight" in gauge


def test_charts_have_an_empty_state(client):
    """A demo whose volume was just created must render empty charts, not broken ones.

    Both daily charts and the distribution take an explicit no-data branch before any
    scale is computed: a max over an empty series is -Infinity, and every bar height
    downstream of it is NaN — which SVG renders as nothing at all, with no clue why.

    MUTATION that must turn this red: delete the `!series.length || series.every(...)`
    guard from renderCostChart.

    WEAK BY CONSTRUCTION: grep-level. It sees that the branch is present and that its
    label is not the string Python stringifies a missing value into; it does not see it
    execute.
    """
    html = client.get("/dashboard").text
    block = _block(html, "charts (SVG)")

    assert "series.every(d => d.runs === 0)" in block, "no empty branch in the cost chart"
    assert "!points.length" in block, "no empty branch in the latency chart"
    assert block.count('class: "empty"') >= 2
    assert "no runs yet" in block
    assert "None" not in html


# --- Wave 5: the drill-down panel (DASH-03) ------------------------------------------
#
# NOTE ON STRENGTH, restated for this wave: there is still no DOM in this suite — no
# jsdom, no headless browser — so nothing below executes openDrill, opens a <dialog> or
# renders a step. These are grep-level regression guards on the served HTML: they see
# that the branch, the field read or the class is present in the shipped block, not that
# a browser does anything with it. The DOM-level proof is deferred to 06-07's human
# checkpoint, which 06-VALIDATION.md already records. Every assertion below is scoped to
# a marker-delimited block via `_block`, and every ABSENCE assertion runs over
# `_code_only`, because the comments in that block name what it must not do on purpose.

_DRILL = "drill-down (/runs/{uid})"


def test_the_runs_table_opens_the_drill_down(client):
    """D-05: a run row is the control that opens that run's panel, keyed on run_uid.

    06-05 stamped `row.dataset.uid` from `last_runs.run_uid` with nothing reading it.
    This is the handle being used: the row carries a control that calls openDrill, and
    openDrill fetches the public JSON route rather than navigating anywhere — the panel
    is a client-rendered view on the same page, not a route split.

    MUTATION that must turn this red: delete the `if (!r.run_uid)` branch from
    runCell — a pre-Phase-5 row (whose uid is null) then gets a control that fetches
    `/runs/null` and opens a dialog reading "no such run", which looks like the page is
    broken rather than like history that predates step recording.

    WEAK BY CONSTRUCTION: grep over served HTML, as the wave header says.
    """
    html = client.get("/dashboard").text
    assert '<dialog id="drill">' in html, "the panel is not a native <dialog> (D-05)"

    drill = _block(html, _DRILL)
    poll = _block(html, "metrics poll (/metrics)")
    drill_code, poll_code = _code_only(drill), _code_only(poll)

    # The table side: the uid is read, and it decides whether a control exists at all.
    assert "r.run_uid" in poll_code
    assert "openDrill(r.run_uid)" in poll_code, "no row opens the drill-down"
    assert "if (!r.run_uid)" in poll_code, (
        "no null-uid branch — a legacy row would get a control that opens nothing"
    )

    # The panel side: one definition, one fetch of the public route, one showModal.
    assert "function openDrill(" in drill_code
    assert 'fetch("/runs/" +' in drill_code, "openDrill does not read GET /runs/{uid}"
    assert "showModal()" in drill_code, "the panel never opens as a modal dialog"
    # A dialog, not a page: nothing here navigates.
    assert "location.href" not in drill_code and "window.open" not in drill_code


def test_the_drill_panel_renders_the_run_states(client):
    """All four documented states are DESIGNED states, and a 404 is a fifth.

    06-04's absence matrix answers `complete | in_flight | swept | unrecorded`, all 200,
    plus one 404 for a uid this service never minted. A panel that only knows "complete"
    renders three of those five as an empty box, which reads as a broken page.

    MUTATION that must turn this red: delete the `"swept"` branch — a 30-day-old run
    then renders as an empty panel, so retention working correctly is indistinguishable
    from the page failing.

    WEAK BY CONSTRUCTION: grep-level; the branch is present, not proven to execute.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _DRILL))

    for status in ("complete", "in_flight", "swept", "unrecorded"):
        assert f'"{status}"' in code, f"the panel has no {status} state"
    # The not-ok branch, with 404 named: it is the only 404 the route emits and it
    # means the uid is unknown, which is a different sentence from "swept".
    assert "resp.ok" in code, "a non-200 response is not handled at all"
    assert "404" in code
    # The swept branch renders the SERVER's note (it names the retention window), not a
    # window length hardcoded here that a settings change would silently falsify.
    assert "d.note" in code
    assert "None" not in html


def test_the_drill_panel_renders_values_as_text_never_html(client):
    """T-06-23: the panel is the widest surface model-influenced strings reach.

    Tool names, argument keys, doc ids, guard names and error reasons all land here.
    The server clamps the tool name to the registry and intersects arg keys with the
    declared schema (06-03), but `doc`, `id`, `denied_by` and the demo branch's raw
    input are strings — so the rendering rule is the control that stands regardless.

    MUTATION that must turn this red: render one step through a markup sink
    (`li.innerHTML = "<div>" + s.tool + "</div>"`) inside the drill block.

    WEAK BY CONSTRUCTION: grep over served HTML. It cannot see a sink reached through
    an alias; the whole-page test above has the same limit and the same value.
    """
    html = client.get("/dashboard").text
    drill = _block(html, _DRILL)

    for sink in _MARKUP_SINKS:
        assert sink not in drill, f"{sink} reached the drill panel — T-06-23"
    assert "textContent" in drill, "the panel writes no value as text at all"
    assert "el(" in drill, "the panel does not build its nodes through the el() helper"


_STEP_TYPES = ("usage", "text", "tool_use", "tool_result",
               "guardrail", "notice", "resolution", "error")


def test_the_drill_panel_renders_every_step_type(client):
    """DASH-03: the trace renders all eight types the server can send, field by field.

    `project_run_detail` publishes a named field list per type and drops anything else.
    A renderer that dispatches on six of them leaves two step types as a bare row with
    no detail — and the panel still looks full, so nothing about the page says it lost
    something.

    MUTATION that must turn this red: drop the `guardrail` branch. The prompt-injection
    denial — a ticket body naming another ticket's id, refused by the ticket-binding
    guard — is the demo's best moment, and it would silently stop rendering while every
    other test on this page stayed green.

    WEAK BY CONSTRUCTION: grep, and specifically a grep for the DISPATCH form
    (`s.type === "x"`) plus each type's own field reads, so a name surviving only in a
    comment cannot make it pass — `_code_only` drops the comments first.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _DRILL))

    for step_type in _STEP_TYPES:
        assert f's.type === "{step_type}"' in code, f"no branch renders a {step_type} step"

    # tool_use: the name and the argument KEYS (never the values — that is what the
    # server sent, and D-01 permits exactly this much).
    for field in ("s.tool", "s.arg_keys", "s.unknown_arg_count"):
        assert field in code, f"the tool call never shows {field}"
    # tool_result: outcome, the guard that refused it, and each tool's own result shape.
    for field in ("s.is_error", "s.denied_by", "s.results",
                  "s.reply_id", "s.escalation_id", "s.category"):
        assert field in code, f"the tool result never shows {field}"
    # guardrail: which guard, what it did, and the ticket-id pair that is the payoff.
    for field in ("s.guard", "s.action", "s.expected_ticket_id",
                  "s.supplied_ticket_id", "s.missing_count"):
        assert field in code, f"the guardrail step never shows {field}"
    # the rest, each named against its own published fields
    for field in ("s.reason", "s.error_type", "s.kind", "s.cause", "s.retrieval_mode",
                  "s.result_count", "s.via", "s.cost_usd", "s.char_count",
                  "s.input_tokens", "s.output_tokens"):
        assert field in code, f"the trace never shows {field}"
    assert "None" not in html


def test_the_drill_panel_distinguishes_cited_from_retrieved(client):
    """The grounding story: what the reply cited, versus what it merely saw.

    The comparison itself is the SERVER's — `project_run_detail` stamps `cited` on each
    search_docs result using `normalise_citation`, the citation guard's own
    normalisation, so the view and the control cannot drift (06-03). This page renders
    that answer and must never recompute it: a highlight that disagrees with the guard
    is worse than no highlight.

    MUTATION that must turn this red: render every chunk identically (drop the
    `cited ?` ternary and use one class and one label) — the panel still looks full of
    grounded retrieval while the only thing that made it legible is gone.

    WEAK BY CONSTRUCTION: grep. It sees two classes, two labels and the flag being
    read; it cannot see the highlight on screen. That is 06-07's human checkpoint.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _DRILL))

    assert "r.cited" in code, "the panel never reads the server's cited flag"
    for field in ("r.doc", "r.id", "r.score"):
        assert field in code, f"a retrieved chunk never shows {field}"
    # Two distinct classes AND two distinct labels — a class alone is invisible if the
    # stylesheet never distinguishes them, and a label alone is easy to miss.
    assert '"chunk cited"' in code and '"chunk uncited"' in code
    assert "cited in the reply" in code and "retrieved, not cited" in code
    assert ".chunk.cited" in html and ".chunk.uncited" in html, (
        "the two chunk states are not styled differently — the class is decorative"
    )
    # ...and the comparison is not re-derived here: the page never reaches into the
    # reply's citation list, which is what the server compared against.
    assert ".citations" not in code
    assert "normalise" not in code


def test_the_drill_panel_renders_timings(client):
    """A missing time renders as a dash, never as 0.

    `elapsed_ms` is the offset from the run's start and `duration_ms` is a tool call's
    own span; both are null for rows written before the elapsed_ms migration (and
    duration_ms is null for a tool_use with no paired result). Rendering null as 0 makes
    a legacy step claim it took no time — a fabricated number on the one page whose job
    is credibility. Rendering it as the word Python prints for an absent value is the
    bug tests/test_auth.py's "None" check exists for.

    MUTATION that must turn this red: collapse the guard to `String(v || 0)` in `dash`
    and `ms` — every null timing becomes a confident 0.

    WEAK BY CONSTRUCTION: grep for the guard's presence, not for its behaviour.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _DRILL))

    assert "s.elapsed_ms" in code, "steps are not timed at all"
    assert "s.duration_ms" in code, "tool calls carry no duration"
    # The one guard both helpers use, asserted as written — a dash, from an explicit
    # null/undefined test rather than from falsiness (0 is a real timing).
    assert code.count('(v === null || v === undefined) ? "—"') >= 2
    assert "None" not in html


def test_the_drill_panel_renders_demo_fidelity_when_present(client):
    """The visitor's own run shows its raw inputs and outputs — and only then.

    The server adds `input`, `result`, `text` and `missing_citations` on the demo branch
    ONLY (D-02, decided from tickets.origin). Every one of them is optional, so the
    renderer must ask before it reads: a redacted run has to render with no empty holes
    where those fields would have been.

    MUTATION that must turn this red: render the raw region unconditionally (drop the
    `if (!raw.length) return;` guard) — every public run grows an empty "raw" section
    that promises detail it does not have.

    WEAK BY CONSTRUCTION: grep. That the region is genuinely collapsed is a browser
    fact; this sees a <details>/<summary> pair being built.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _DRILL))

    for field in ("s.input", "s.result", "s.text", "s.missing_citations"):
        assert field in code, f"a demo run never shows {field}"
    # Secondary and collapsed, not inline with the trace.
    assert '"details"' in code and '"summary"' in code
    # Optional, every one of them: presence is tested before anything is built...
    assert "!== undefined" in code
    # ...and nothing is appended at all when the server sent none of them.
    assert "if (!raw.length) return;" in code
