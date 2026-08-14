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
from html import unescape
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
# lookup_customer result -> recent_tickets[].subject. Its OWN sentinel, and it used to
# be DETAIL_REASON: the fixture reused the visitor's escalation reason as a third party's
# earlier subject, which made those two vectors one string and left no test in this file
# able to tell "the demo branch keeps what the visitor wrote" apart from "the demo branch
# keeps what the service looked up". They are opposite requirements.
DETAIL_PRIOR_SUBJECT = "DRILLDOWN-SOMEONE-ELSES-SUBJECT-2b7c"
# lookup_customer result -> customer.name, and restated in model prose.
DETAIL_CUSTOMER_NAME = "DRILLDOWN-CUSTOMER-NAME-5f3a"

DETAIL_SENTINELS = (
    ("customer email", DETAIL_EMAIL),
    ("search query", DETAIL_QUERY),
    ("retrieved prose", DETAIL_PROSE),
    ("escalation reason", DETAIL_REASON),
    ("reply body", DETAIL_REPLY),
    ("missing citation", DETAIL_CITE),
    ("tool error message", DETAIL_ERROR),
    ("another visitor's earlier subject", DETAIL_PRIOR_SUBJECT),
    ("the looked-up customer's name", DETAIL_CUSTOMER_NAME),
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
            "customer": {
                "email": DETAIL_EMAIL, "name": DETAIL_CUSTOMER_NAME, "plan": "enterprise",
            },
            "recent_tickets": [
                {"id": 1, "subject": DETAIL_PRIOR_SUBJECT, "status": "open"},
            ],
        }}, 55),
        # The vector CR-01's fix missed: the model READS that result and restates it, in
        # exactly the terms the system prompt asks for ("so you know their plan and
        # history", "Address the customer by name"). This is prose, so no field allowlist
        # can reach it — the demo branch publishes `text` whole.
        ("text", {"text": (
            f"{DETAIL_CUSTOMER_NAME} is on the enterprise plan. Their recent tickets"
            f" include '{DETAIL_PRIOR_SUBJECT}'."
        )}, 56),
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

    The demo branch is an allowlist on a SECOND axis too (CR-01): raw payloads are
    published per tool, from `_DEMO_RAW_TOOLS`, and `lookup_customer` is on neither
    side of it — its result is a stored record about a third party, not the visitor's
    own content. So `DETAIL_EMAIL`, which rides only that tool's input and result, must
    be ABSENT from both branches while every visitor-authored sentinel stays present.

    And on a THIRD axis, which is the one the phase-6 verification found open: the model
    READS that redacted result and restates it in prose, and the demo branch publishes
    prose whole. `DETAIL_CUSTOMER_NAME` and `DETAIL_PRIOR_SUBJECT` ride the lookup's
    result AND a `text` step that names them both, so they can only be absent below if
    the value mask is derived from what this run's non-allowlisted tools RETURNED. A
    per-field allowlist cannot reach them and neither can the route's one address
    literal.

    MUTATION that must turn this red: drop the `and raw_tool in _DEMO_RAW_TOOLS` /
    `and payload.get("tool") in _DEMO_RAW_TOOLS` conditions in project_run_detail — the
    demo branch republishes the customer row and the first assertion below fires.

    SECOND MUTATION (the prose vector): make `prose_withheld` just `withheld` — i.e.
    delete the `withheld_from_run(parsed, ...)` term. The raw payloads stay redacted,
    every other assertion here stays green, and the name and the earlier subject come
    back out through the `text` step.
    """
    rows = _store(conn, _leaky_run_events())
    known = _known_tools(registry)

    public = project_run_detail(rows, full_fidelity=False, known_tools=known)
    demo = project_run_detail(rows, full_fidelity=True, known_tools=known)

    demo_json = json.dumps(demo, default=str)
    # Anti-vacuity for the two prose sentinels: they really are in this run's rows, and
    # really do reach a field the demo branch publishes.
    raw = "".join(r["payload"] for r in rows)
    for name, sentinel in (
        ("looked-up customer name", DETAIL_CUSTOMER_NAME),
        ("another visitor's earlier subject", DETAIL_PRIOR_SUBJECT),
    ):
        assert raw.count(sentinel) == 2, (
            f"the {name} sentinel must ride BOTH the lookup result and the model's prose"
        )
    assert any(s["type"] == "text" and s.get("text") for s in demo), (
        "the demo branch published no prose at all — the absence assertions below would"
        " be vacuous"
    )
    # The third party's address rides lookup_customer's input AND its result, and
    # nothing else in this run — so its absence is a claim about that tool being off
    # the raw allowlist, on the branch that is supposed to be the fullest.
    assert DETAIL_EMAIL not in demo_json, (
        "the demo branch republished the looked-up customer's address (CR-01)"
    )
    # The prose vector, by value: the model's own sentence names both, and the whole
    # point is that no field-level rule could have stopped it.
    assert DETAIL_CUSTOMER_NAME not in demo_json, (
        "the model's prose republished the looked-up customer's name"
    )
    assert DETAIL_PRIOR_SUBJECT not in demo_json, (
        "the model's prose republished another visitor's ticket subject"
    )
    # ...and what replaced them is the mask, not a step that quietly vanished.
    assert "[withheld]" in demo_json, "the prose lost its content instead of its secrets"
    lookup_use = next(
        s for s in demo if s["type"] == "tool_use" and s.get("tool") == "lookup_customer"
    )
    lookup_result = next(
        s for s in demo if s["type"] == "tool_result" and s.get("tool") == "lookup_customer"
    )
    assert "input" not in lookup_use, "lookup_customer's raw input is on the demo branch"
    assert "result" not in lookup_result, "lookup_customer's raw result is on the demo branch"
    # ...and the redacted shape is still there, so this is redaction and not omission.
    assert lookup_use["arg_keys"] == ["email"]

    for name, sentinel in (
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


def test_a_drill_down_flood_leaves_the_live_feed_connectable(client, monkeypatch):
    """WR-02: the drill-down's own bucket governs the drill-down, and NOTHING else does.

    This is the property `anon_run_detail_limit` was added for, stated in three places
    (main.py, ratelimit.py, config.py) and untested until now: "a visitor clicking
    through the back catalogue would otherwise spend the live feed's reconnect allowance
    and silently break their own feed." `_gate` charged the shared `auth` bucket first on
    every public route, and with the shipped defaults that bucket (60/minute) is SMALLER
    than the drill-down's own (120/minute) — so the drill-down's bucket could never bind,
    and a 60-open flood took /events down with it. EventSource treats a non-200 as
    terminal, so that is a dead feed and a "reload to watch again" page.

    MUTATION that must turn this red (it is the shipped code): put
    `await enforce("auth", "anon", request)` back above the `if public:` branch in
    `_gate._dependency`. The third GET below then 429s from the `auth` bucket — not the
    route's — and the /events connect that follows is refused too.

    The limits are inverted from production on purpose: `auth` is made TINY and the
    drill-down's own bucket huge, so anything the flood spends other than its own bucket
    is what fails. `test_run_detail_is_rate_limited_per_ip` is the complement — it proves
    the route's own bucket still meters it.
    """
    monkeypatch.setattr(settings, "anon_auth_limit", "2/minute")
    monkeypatch.setattr(settings, "anon_run_detail_limit", "1000/minute")
    # So the admitted stream closes on its own instead of holding the TestClient open
    # for the idle ceiling — this test is about the connect, not the stream.
    monkeypatch.setattr(settings, "events_heartbeat_seconds", 0.01)
    monkeypatch.setattr(settings, "events_idle_seconds", 0.02)

    uid = "e" * 32
    with _anon(client) as anon:
        codes = [anon.get(f"/runs/{uid}").status_code for _ in range(5)]
        feed = anon.get("/events")

    # The feed FIRST: it is the property this test is named for, and asserting it ahead
    # of the flood's own status codes means a regression reports the broken feed rather
    # than the 429 that preceded it.
    assert feed.status_code == 200, (
        "the live feed was refused after a drill-down flood — the isolation Phase 5's"
        f" CR-01 established (drill-down codes were {codes})"
    )
    assert feed.headers["content-type"].startswith("text/event-stream")
    # And every open answered from the route's own generous bucket. A 429 anywhere in
    # here is another route's bucket binding, which is the same bug seen from its cause.
    assert codes == [404] * 5, f"a drill-down open was refused by another route's bucket: {codes}"


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
# An EARLIER ticket's subject, filed against the same address by someone else (CR-02).
# It rides lookup_customer's `recent_tickets` and nothing else, so it is the only
# sentinel that can see that vector: a fix which redacts the `customer` object and
# leaves the ten subjects beside it passes every other assertion in this file.
DRILL_SUBJECT = "SENTINEL-EARLIER-TICKET-SUBJECT-5a3d81"

DRILL_SENTINELS = (
    ("customer email", DRILL_EMAIL),
    ("ticket body", DRILL_BODY),
    ("api key", DRILL_KEY),
    ("fabricated citation", DRILL_CITE),
    ("earlier ticket subject", DRILL_SUBJECT),
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
        # string in model prose so the text branch is exercised on the way past. The
        # prose also NAMES THE ADDRESS the model just looked up, because that is what a
        # real reply does — and on the demo branch prose is published raw, so this is
        # the vector no field-level allowlist can close (CR-01's value mask closes it).
        response([
            text_block(f"The customer {DRILL_EMAIL} wrote: {DRILL_BODY}"),
            tool_use_block("create_escalation", {
                "ticket_id": ticket_id,
                "reason": f"customer reported: {DRILL_BODY} — needs a human review",
                "priority": "high",
            }),
        ]),
        response([text_block("escalated")], stop_reason="end_turn"),
    ])


def _seed_the_looked_up_customer() -> None:
    """A real customers row AND an earlier ticket of theirs, so lookup_customer returns
    what it returns in production: a whole record — email, name, plan — plus up to ten
    of that address's ticket subjects.

    The earlier ticket is the CR-02 half. Those subjects belong to whoever else has been
    filing against this address (on the deployed service, the owner key), so they are
    third-party content that no visitor authored, and until this row existed no test in
    this suite could see that vector at all.
    """
    app.state.conn.execute(
        "INSERT INTO customers (email, name, plan, signed_up) VALUES (?, ?, ?, ?)",
        (DRILL_EMAIL, "Drill Sentinel", "enterprise", "2025-01-01"),
    )
    app.state.conn.execute(
        "INSERT INTO tickets (customer_email, subject, body, origin) VALUES (?, ?, ?, ?)",
        (DRILL_EMAIL, DRILL_SUBJECT, "filed by someone else, long before this run", None),
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
    """D-02's INVERSE: the Try-it visitor gets the raw trace of their OWN run — and
    only of their OWN (CR-01, CR-02).

    Without the first half, "full fidelity for demo runs" is untested and can silently
    regress to redacted-for-everyone: the drill-down would still pass every leak test
    above, and the whole payoff of the Try-it flow would be gone with nothing noticing.

    THE PRODUCTION SHAPE, and the reason this test was rewritten. The ticket's own
    address IS the address the model looks up, because that is what the Try-it form
    produces — it pins each example to a seeded customer and the system prompt has the
    agent call lookup_customer first. The previous version gave the ticket a DIFFERENT
    address from the one the run looked up, so its `assert ticket_address not in ...`
    was true of a string that appeared nowhere in the run at all: a fixture artifact,
    not a property. It also asserted `lookup_result["result"]["customer"]["email"] ==
    DRILL_EMAIL`, which CERTIFIED the disclosure and would have gone red on the fix.

    What the visitor authored stays full fidelity — their ticket text, the model's
    prose, its tool arguments, its citations — and that half is asserted first, because
    a projector that published nothing would satisfy every absence assertion below.

    What the SERVICE looked up about someone else does not, by VALUE and not by column
    name: `assert "customer_email" not in json.dumps(detail)` greps for a key the
    address is not published under (`lookup_customer` names it `email`), so it could
    never have seen this.

    MUTATION 1 (the shipped code, before CR-01): drop `and raw_tool in _DEMO_RAW_TOOLS`
    / `and payload.get("tool") in _DEMO_RAW_TOOLS` from project_run_detail — the raw
    customer row and its ten ticket subjects come back and the address/subject
    assertions fire.
    MUTATION 2 (the prose vector): make `prose_withheld` just `withheld` in
    project_run_detail — i.e. drop the run-derived term. The model's own sentence names
    the address it just read, and the assertion fires from a different field than
    mutation 1 does.

    NOT A MUTATION OF THIS TEST ANY MORE, recorded because the docstring used to claim
    it was: passing `withheld=()` from the route leaves this green. The address is in
    `lookup_customer`'s RESULT here, so the run-derived mask covers it whatever the route
    passes. What the route's literal alone still carries is the run whose lookup MISSED —
    `test_a_demo_run_whose_lookup_missed_still_withholds_the_address` below is that case,
    and it is where that mutation reds.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    _seed_the_looked_up_customer()
    # The DEMO key, so tickets.origin is 'demo' — the whole difference from the leak
    # test above is which credential posted /tickets. The address is DRILL_EMAIL: the
    # ticket's own customer and the one the agent looks up are THE SAME ADDRESS, which
    # is the shape the Try-it form actually produces.
    ticket_id = _demo_ticket(
        client, DRILL_EMAIL, f"my key {DRILL_KEY} stopped working. {DRILL_BODY}"
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

    # --- the visitor's OWN content, in full: this half is D-02's payoff --------------
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

    # --- and NOTHING the service looked up about anyone else (CR-01, CR-02) ---------
    whole = json.dumps(detail)
    # By VALUE, and asserted BEFORE the shape assertions below so a regression reports
    # the disclosure itself rather than the mechanism that was supposed to prevent it.
    # This address is the ticket's own AND the one the model looked up AND the one the
    # model's prose names, so it is reachable through three different fields; the
    # assertion is about the response, not about any one of them.
    assert DRILL_EMAIL not in whole, (
        "a third party's address is on the keyless public route"
    )
    # The subjects beside the customer row are an independent vector: a fix that
    # redacted `customer` and left `recent_tickets` would pass the line above.
    assert DRILL_SUBJECT not in whole, (
        "another person's ticket subject is on the keyless public route"
    )
    # The key name too, kept from the old version — it is a real (if narrower) guard on
    # the `tickets` column, and it is NOT what the two assertions above check.
    assert "customer_email" not in whole

    lookup = next(s for s in steps if s["type"] == "tool_use" and s["tool"] == "lookup_customer")
    # lookup_customer is off the demo branch's raw allowlist, so its input and its
    # result are redacted here exactly as they are for everyone else — while the
    # redacted SHAPE stays, so this is redaction and not omission.
    assert "input" not in lookup and lookup["arg_keys"] == ["email"]
    lookup_result = next(
        s for s in steps if s["type"] == "tool_result" and s["tool"] == "lookup_customer"
    )
    assert "result" not in lookup_result and lookup_result["is_error"] is False
    # Anti-vacuity for the absence half: the address was genuinely in this run's prose,
    # and what replaced it is the mask rather than a step that vanished.
    prose = next(s["text"] for s in steps if s["type"] == "text" and s.get("text"))
    assert "[withheld]" in prose, "the prose step lost its content instead of its secret"


# --- the prose vector: what the SERVICE looked up, restated by the model --------------
#
# The residual 06-VERIFICATION reproduced with zero credentials. CR-01 closed the raw
# payload (lookup_customer off _DEMO_RAW_TOOLS) and the email literal (the route's
# `withheld`); it did not close the model RESTATING the lookup — which is not an edge
# case but the behaviour prompts.py asks for by name ("so you know their plan and
# history", "Address the customer by name").
#
# Sentinels are their own set rather than the DRILL_ ones because the property under
# test is different: these must ride the LOOKUP'S RESULT and the model's prose and
# nothing the visitor wrote, so that their absence can only be explained by a mask
# derived from what the run's non-allowlisted tools returned.
PROSE_EMAIL = "prose-sentinel-1c4d@example.com"
PROSE_NAME = "PROSE-CUSTOMER-NAME-77b2"                    # customers.name
PROSE_OTHER_SUBJECT = "PROSE-ANOTHER-VISITORS-SUBJECT-9d3e"  # someone else's ticket
# The visitor's OWN subject, which lookup_customer hands back too (it selects the last
# ten tickets for the address, and this run's ticket is one of them). It must survive:
# withholding a visitor's own words from their own trace is not a security property.
PROSE_OWN_SUBJECT = "Refund for yesterday's charge"
PROSE_BODY = "PROSE-VISITOR-BODY-5a8f"                     # the visitor's own words
# The plan is "pro" on purpose. It is what the deployed seed data actually holds, it is
# three characters, and it is a substring of ordinary English — so it is the value that
# decides whether the mask can be both complete and legible.
PROSE_PLAN = "pro"


def _seed_the_prose_customer() -> None:
    """A customers row and an earlier ticket filed against it by SOMEONE ELSE."""
    app.state.conn.execute(
        "INSERT INTO customers (email, name, plan, signed_up) VALUES (?, ?, ?, ?)",
        (PROSE_EMAIL, PROSE_NAME, PROSE_PLAN, "2025-01-01"),
    )
    app.state.conn.execute(
        "INSERT INTO tickets (customer_email, subject, body, origin) VALUES (?, ?, ?, ?)",
        (PROSE_EMAIL, PROSE_OTHER_SUBJECT, "filed by another visitor", "demo"),
    )
    app.state.conn.commit()


def _script_the_prose_restatement_run(ticket_id: int) -> None:
    """A run that looks the customer up and then writes down what it read.

    Nothing here is contrived: this is the shape the system prompt asks for, and the
    verifier's live reproduction produced the same sentence from the real model.
    """
    app.state.client = FakeClient([
        response([tool_use_block("lookup_customer", {"email": PROSE_EMAIL})]),
        response([
            text_block(
                f"{PROSE_NAME} is on the {PROSE_PLAN} plan. Their recent tickets include"
                f" '{PROSE_OTHER_SUBJECT}'. I am processing '{PROSE_OWN_SUBJECT}' now."
            ),
            tool_use_block("create_escalation", {
                "ticket_id": ticket_id,
                "reason": (
                    f"{PROSE_NAME} ({PROSE_PLAN} plan) also wrote in about"
                    f" '{PROSE_OTHER_SUBJECT}'. This ticket: {PROSE_BODY}"
                ),
                "priority": "high",
            }),
        ]),
        response([text_block("escalated")], stop_reason="end_turn"),
    ])


def test_a_demo_runs_prose_cannot_republish_what_the_run_looked_up(client, monkeypatch):
    """CR-01's residual: the model's own words are the third vector, and they are closed
    by deriving the mask from what THIS RUN'S non-allowlisted tools returned.

    THE ATTACK, exactly as 06-VERIFICATION reproduced it and in the same order: no
    credential at all -> GET /metrics -> harvest a run_uid -> GET /runs/{uid} -> another
    visitor's ticket subject, plus the looked-up customer's name and plan. Every step
    below is that walk, which is why the uid comes out of /metrics rather than out of the
    submitter's response header: the header is what the VISITOR holds, and the point is
    that a stranger does not need it.

    WHY A FIELD ALLOWLIST CANNOT DO THIS. `lookup_customer` is already off the demo
    branch's raw allowlist, so its input and result are redacted — and the model reads
    the result anyway, because the tool returned it to the model before any of this ran.
    Prose is the model restating it. There is no field to deny.

    WHAT IS PROVED HERE: that these literals do not survive. What is NOT proved, in this
    test or anywhere in this suite, is that the GIST does not — a paraphrase ("the
    customer on our top tier") shares no substring with the value and no mask sees it.
    That limit is stated in `mask_withheld` and `project_run_detail`, and it is the
    reason this is a floor rather than a proof.

    MUTATION 1 (the fix itself): in `project_run_detail`, make `prose_withheld` just
    `withheld` — drop the `withheld_from_run(parsed, ...)` term. The raw payloads stay
    redacted, the shipped test above stays green, and the name, the plan and the other
    visitor's subject all come back through `text` and the escalation reason.

    MUTATION 2 (the visitor's own words): drop the `authored=` argument at the route's
    call site. The absence half still passes and the "the visitor's own subject
    survives" assertion reds — the demo would publish "[withheld]" where the visitor's
    own subject stood, which is D-02's payoff replaced by a redaction of the visitor to
    themselves.

    MUTATION 3 (legibility): make `_mask_pattern` a bare `re.escape(secret)` with no
    word boundaries. "processing" becomes "[withheld]cessing" and the legibility
    assertion reds — a mask that shreds ordinary English is one nobody keeps.

    MUTATION 4 (the short value): raise `_MIN_WITHHOLD_LEN` to 4. "pro" is no longer
    collected and the plan assertion reds — three characters is the length at which the
    deployed seed data's own plan names live.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    _seed_the_prose_customer()
    ticket_id = _demo_ticket(
        client,
        PROSE_EMAIL,
        f"I was charged twice. {PROSE_BODY}",
        subject=PROSE_OWN_SUBJECT,
    )
    assert _origin_of(ticket_id) == "demo"
    _script_the_prose_restatement_run(ticket_id)
    resp = client.post(f"/tickets/{ticket_id}/process")
    assert "event: error" not in resp.text
    submitter_uid = resp.headers["X-Relay-Run-Uid"]

    # --- anti-vacuity: the sentinels really are in the raw rows, twice over ----------
    # Once because the tool RETURNED them and once because the model RESTATED them. If
    # either half were missing the absence assertions below would be about a string that
    # was never in the run.
    rows = app.state.conn.execute(
        "SELECT type, payload FROM run_events WHERE run_uid = ?", (submitter_uid,)
    ).fetchall()
    raw_by_type: dict[str, str] = {}
    for row in rows:
        raw_by_type[row["type"]] = raw_by_type.get(row["type"], "") + row["payload"]
    for name, sentinel in (
        ("the looked-up customer's name", PROSE_NAME),
        ("another visitor's ticket subject", PROSE_OTHER_SUBJECT),
    ):
        assert sentinel in raw_by_type.get("tool_result", ""), (
            f"{name} never reached the lookup's stored result"
        )
        assert sentinel in raw_by_type.get("text", ""), (
            f"{name} was never restated in the model's prose — the vector is not armed"
        )
    assert PROSE_PLAN in raw_by_type.get("text", "")

    # --- the walk: no credential, /metrics, then the run it names ---------------------
    with _anon(client) as anon:
        metrics = anon.get("/metrics")
        assert metrics.status_code == 200, "the enumeration surface is not even public"
        harvested = [r["run_uid"] for r in metrics.json()["last_runs"] if r.get("run_uid")]
        assert submitter_uid in harvested, (
            "the uid is not anonymously enumerable — this test would be checking a route"
            " nobody can reach rather than the one the reproduction used"
        )
        detail = anon.get(f"/runs/{submitter_uid}")
    assert detail.status_code == 200
    body = detail.json()
    whole = json.dumps(body)

    # --- the disclosure, closed -------------------------------------------------------
    assert body["demo"] is True, "not the full-fidelity branch — the test proves nothing"
    for name, sentinel in (
        ("the looked-up customer's name", PROSE_NAME),
        ("another visitor's ticket subject", PROSE_OTHER_SUBJECT),
    ):
        assert sentinel not in whole, (
            f"{name} is on the keyless public route, restated by the model"
        )
    # The plan, asserted as a WORD over the fields the model authored rather than over
    # the whole document: "pro" occurs inside ordinary words, and an absence assertion
    # that a knowledge-base chunk could break would say nothing about this vector.
    authored_fields = [s["text"] for s in body["steps"] if s.get("text")]
    authored_fields += [
        s["input"]["reason"] for s in body["steps"]
        if s.get("tool") == "create_escalation" and isinstance(s.get("input"), dict)
    ]
    assert authored_fields, "no model-authored field was published — vacuous"
    for field in authored_fields:
        assert not re.search(rf"\b{PROSE_PLAN}\b", field), (
            f"the looked-up customer's plan survived in model-authored text: {field!r}"
        )

    # --- and D-02's payoff is intact --------------------------------------------------
    # Redaction, not deletion: what the visitor wrote is still theirs to read.
    assert "[withheld]" in whole, "the prose lost its content instead of its secrets"
    assert PROSE_BODY in whole, "the visitor's own words were withheld from the visitor"
    prose = " ".join(authored_fields)
    assert PROSE_OWN_SUBJECT in prose, (
        "the visitor's OWN subject was masked out of their own trace — lookup_customer"
        " returns it among the address's last ten tickets, and it is not third-party"
    )
    # The mask is a token mask, not a substring shredder: ordinary English survives.
    assert "processing" in prose, (
        "masking the plan rewrote an unrelated word — a drill-down full of spurious"
        " [withheld] markers teaches a visitor to ignore the real ones"
    )


def test_a_demo_run_whose_lookup_missed_still_withholds_the_address(client, monkeypatch):
    """The one literal the RUN's rows cannot supply: an address nothing looked up.

    `lookup_customer` answers `{"found": false}` for any address without a customers row,
    so nothing this run stored carries it — and the model still has it, because
    `ticket_prompt` puts "From: <address>" in the message it was given. The run-derived
    mask cannot see that value by construction; the route's own `withheld` literal is the
    only thing standing between it and a keyless route.

    This is why `run_detail` still passes one literal after the mask moved into the
    projector, and it is the case that keeps that argument honest — the shipped
    full-fidelity test above cannot, because there the lookup HITS and the run-derived
    mask covers the address too.

    MUTATION that must turn this red: delete the `withheld = (ticket["customer_email"],)`
    assignment in `run_detail`. The address the visitor typed is republished from the
    model's prose on the anonymous route, exactly as it was before CR-01.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    unseeded = "nobody-has-this-row-4b1e@example.com"
    assert app.state.conn.execute(
        "SELECT 1 FROM customers WHERE email = ?", (unseeded,)
    ).fetchone() is None, "the address must be unseeded or the lookup would hit"

    ticket_id = _demo_ticket(client, unseeded, f"charged twice. {PROSE_BODY}")
    app.state.client = FakeClient([
        response([tool_use_block("lookup_customer", {"email": unseeded})]),
        response([
            text_block(f"No record for {unseeded}. Escalating: {PROSE_BODY}"),
            tool_use_block("create_escalation", {
                "ticket_id": ticket_id,
                "reason": f"unknown customer {unseeded} reports: {PROSE_BODY}",
                "priority": "high",
            }),
        ]),
        response([text_block("escalated")], stop_reason="end_turn"),
    ])
    resp = client.post(f"/tickets/{ticket_id}/process")
    assert "event: error" not in resp.text
    uid = resp.headers["X-Relay-Run-Uid"]

    # Anti-vacuity, both halves: the lookup really did MISS (so the harvest has nothing
    # to find), and the address really is in the model's prose.
    rows = app.state.conn.execute(
        "SELECT type, payload FROM run_events WHERE run_uid = ?", (uid,)
    ).fetchall()
    results = [r["payload"] for r in rows if r["type"] == "tool_result"]
    assert any('"found": false' in p for p in results), "the lookup did not miss"
    assert all(unseeded not in p for p in results), (
        "a tool result carried the address — the run-derived mask would cover it and"
        " this test would not be about the route's literal"
    )
    assert any(unseeded in r["payload"] for r in rows if r["type"] == "text"), (
        "the address was never restated in prose — the vector is not armed"
    )

    with _anon(client) as anon:
        body = anon.get(f"/runs/{uid}").json()
    whole = json.dumps(body)
    assert body["demo"] is True
    assert unseeded not in whole, (
        "a visitor-typed address is on the keyless public route, out of the model's prose"
    )
    assert "[withheld]" in whole, "the prose lost its content instead of its secret"
    assert PROSE_BODY in whole, "the visitor's own words were withheld from the visitor"


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
    assert '"__RELAY_DEMO_KEY_JS__"' in raw, "the script placeholder is gone from disk"

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    # Two placeholders because there are two parsing contexts (WR-05); for a key with
    # no metacharacters both substitutions produce the same text, which is why this
    # test is not the one that proves they are escaped differently.
    assert resp.text == (
        raw
        .replace('"__RELAY_DEMO_KEY_JS__"', '"test-demo-key"')
        .replace("__RELAY_DEMO_KEY__", "test-demo-key")
    )


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


# Every character that one escaper gets wrong in the other context: `&"'<>` are what
# html.escape() encodes (correct in the <code> block, fatal in the script), and the
# trailing backslash is what it does NOT encode (harmless in the <code> block, and the
# character that escapes the closing quote of a JS string literal and kills the page).
_METACHAR_DEMO_KEY = "k&y\"'<s>\\"


def test_the_demo_key_is_escaped_for_the_context_it_lands_in(client, monkeypatch):
    """WR-05. One key, two parsing contexts, two escapers — decoded, not grepped.

    The failure this pins is silent by construction: with a single html.escape() the
    page DISPLAYS the right key while the script holds `k&amp;y`, so every Try-it
    submission 401s and nothing on the server or the page says why. So neither
    assertion below greps for a rendered form; each one runs the decoder the browser
    would run for that context and compares against the configured key.

    MUTATION (executed): restore the single-escaper substitution in `dashboard()` —
    `published = escape(settings.demo_key)` into both placeholders. The HTML
    assertion stays green; the JS assertion reds with the entity-encoded key.
    """
    monkeypatch.setattr(settings, "demo_key", _METACHAR_DEMO_KEY)
    page = client.get("/dashboard").text

    # HTML text-node context: the browser decodes entity references here.
    shown = re.search(r"<code>X-API-Key: (.*?)</code>", page)
    assert shown is not None, "the published-key <code> block is gone"
    assert unescape(shown.group(1)) == _METACHAR_DEMO_KEY

    # Script context: `<script>` is raw text, so the browser decodes NOTHING before the
    # JS parser sees it. json.loads is the same string-literal grammar the JS parser
    # applies (\\uXXXX included), so a literal that survives this is one the browser
    # reads back as the key — and one that does not parse at all reds here too, which
    # is the backslash half of the finding.
    literal = re.search(r"const DEMO_KEY = (.*);", page)
    assert literal is not None, "the DEMO_KEY assignment is gone"
    assert json.loads(literal.group(1)) == _METACHAR_DEMO_KEY

    # Anti-vacuity: the two contexts must actually have been given DIFFERENT text. If
    # this ever holds, one escaper is feeding both again and the assertions above are
    # agreeing by accident.
    assert shown.group(1) != json.loads(literal.group(1))

    # `</script>` cannot be written from inside the literal, whatever the key is.
    monkeypatch.setattr(settings, "demo_key", "a</script><b>x")
    scripted = client.get("/dashboard").text
    js = re.search(r"const DEMO_KEY = (.*);", scripted)
    assert "</script>" not in js.group(1)
    assert json.loads(js.group(1)) == "a</script><b>x"


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


def _el_body(html: str) -> str:
    """The source of `el()` alone, from the render-helpers block.

    Scoped to the ONE function, not the block and not the page: `textContent` appears in
    `svg()`, in `line.textContent`, in `feedStatus.textContent` and in
    `drillTitle.textContent`, so a page-wide grep for the token survives el() losing its
    only rendering statement — which is WR-08.
    """
    helpers = _block(html, "render helpers")
    assert "function el(" in helpers, "el() is not in the render-helpers block any more"
    return helpers.split("function el(", 1)[1].split("\n}", 1)[0]


def test_el_writes_its_text_argument_as_text(client):
    """WR-08: `el()` renders. Every card, chart label, chip, step line and drill-down
    fact on this page is built by it, so this one statement is the page's whole visible
    output — and until now nothing in the suite could see it go.

    NAMED MUTATION this closes (a plausible refactor, not an adversarial alias): replace
    `n.textContent = text` with `n.setAttribute("title", text)`. Every rendered value on
    the page disappears while the whole 407-test suite, including
    `test_dashboard_never_renders_through_a_markup_sink` above, stays green — the token
    `textContent` survives in four other places and no markup sink appears.

    WEAK BY CONSTRUCTION, and specifically weaker than it looks: this is still a grep,
    scoped to one function's source. It proves that el() contains an assignment of its
    `text` parameter to `.textContent`; it does NOT prove a browser calls el(), that
    callers pass a value, or that anything is on screen. There is no DOM in this suite
    and adding one would make Node a test dependency (ruled out by the threat register),
    so the rendering claim itself stays a human check — 06-07's checkpoint. What this
    closes is the specific class where the page's only rendering statement can be
    deleted in silence.
    """
    body = _el_body(client.get("/dashboard").text)

    # The assignment, not the token: `.textContent = text` and nothing weaker.
    assert re.search(r"\.textContent\s*=\s*text\b", body), (
        "el() no longer writes its `text` argument through textContent — every value"
        " the page renders would be invisible, and nothing else in this suite sees it"
    )
    # ...and the parameter it writes is genuinely el()'s third argument, so a rename
    # cannot leave this matching some other `text` in scope.
    assert re.match(r"\s*tag\s*,\s*attrs\s*,\s*text\s*\)", body), body[:80]


def _ci_workflow() -> str:
    path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
    assert path.exists(), f"the CI workflow moved: {path}"
    return path.read_text(encoding="utf-8")


def test_the_docker_smoke_greps_for_content_only_this_page_has(client):
    """WR-08: the container smoke asserts the PAGE, not just that something answered.

    `curl -sf /dashboard | grep -q "Relay"` is satisfied by `<title>Relay dashboard</title>`
    alone — so a build that served `<html><head><title>Relay dashboard</title></head>
    <body></body></html>` printed "smoke ok". The smoke exists to catch a *served but
    broken* page (a missing template is already caught by /health never coming up), and
    that is exactly what a title match cannot see.

    This test is the link between the two files: the tokens the workflow greps for must
    be tokens the served page actually has, so neither can rot without the other going
    red. MUTATION: rename `id="try-examples"` in the template — this fails here rather
    than as a confusing red X in CI on main.
    """
    html = client.get("/dashboard").text
    workflow = _ci_workflow()

    # The workflow greps for these as FIXED strings (`grep -qF`), so the token in the
    # YAML and the token in the page are byte-for-byte the same thing.
    for token in ('id="try-examples"', "openDrill"):
        assert token in workflow, (
            f"the docker smoke no longer greps for {token} — it is back to a check that"
            " a bare <title> would satisfy"
        )
        assert token in html, f"the served page has no {token} for the smoke to find"
    # And the migration path: the smoke must start the image a second time against a
    # database that already exists, which is the only place CI exercises
    # _add_column_if_missing against a pre-existing table (db.py).
    assert "docker volume create" in workflow, (
        "the docker smoke runs with no volume, so a second start against an existing"
        " database — the whole point of _add_column_if_missing — is never exercised"
    )


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

    # WR-09: the two percentile cards are computed over the chart's window, so their
    # labels have to say which window — from the SERVER's number. A literal here would
    # keep printing "last 14d" the day metrics_window_days moves, which is the same
    # class of quiet lie the finding was about.
    code = _code_only(block)
    assert "m.latency_ms.window_days" in code, "the p50 card does not name its window"
    assert not re.search(r'"p50 ms"|"p95 ms"', code), (
        "a percentile card is labelled with no population at all"
    )
    assert not re.search(r"last 14d|last 14 days", code), (
        "the window length is hardcoded beside the setting that defines it"
    )


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


def test_the_gauge_explains_an_idle_day(client):
    """A hollow ring is honest and unreadable: $0 today looks exactly like a load error.

    The scale-to-zero demo spends nothing on a quiet day, so the gauge's usual render
    is a grey track with no fill. This pins the copy that makes that a reading rather
    than a symptom — and pins that it stayed COPY: the branch compares the server's
    `spent` and adds a sentence, it does not compute a fraction, sum `last_runs`, or
    substitute a floor for the empty arc (D-11 — the gauge and the spend gate must be
    incapable of disagreeing).

    MUTATION that must turn this red: delete the `if (spent === 0)` branch — the idle
    gauge goes back to an unexplained grey ring.

    WEAK BY CONSTRUCTION: grep-level. No DOM, no arc is drawn or measured here; this
    sees the branch and its copy in the served document.
    """
    html = client.get("/dashboard").text
    gauge = _block(html, "budget gauge")
    code = _code_only(gauge)

    assert "if (spent === 0) {" in code, "no idle branch in the gauge"
    assert "Nothing spent yet today" in code, "the idle gauge is unexplained"
    # Still copy only: the arithmetic below the branch is the server's, unchanged.
    assert "last_runs" not in code and "reduce(" not in code
    assert code.count("fraction =") == 1, "the idle branch re-derives the fraction"
    # The empty arc is still empty, and the one clamp is still the server's two numbers
    # bounded to [0, 1] — pinned literally, because the tempting way to make an idle
    # gauge look alive is a minimum fill (`Math.max(0.05, ...)`), which draws spend
    # that did not happen on the page whose whole claim is that its numbers are real.
    assert "Math.min(1, Math.max(0, spent / ceiling))" in code, (
        "the fraction is no longer the server's spend over the server's ceiling"
    )
    assert "if (fraction > 0) {" in code, "the fill is no longer gated on real spend"
    assert "None" not in html


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


def test_the_latency_chart_explains_a_single_day_of_runs(client):
    """The sparse render is the demo's NORMAL render, so it has to read as deliberate.

    `min_machines_running = 0` plus real traffic means a 14-day window usually holds
    one busy day. The drawing rule then does the right thing and draws no segment — a
    line across an idle fortnight would be an invention — but a visitor sees two dots
    in an empty box, which reads as a broken chart on the one page whose job is
    credibility. This pins the caption that names the cause, and pins that it did NOT
    buy legibility by inventing the line.

    The guard is asserted as `points.some(` over `adjacent`, not as a hand-rolled
    index comparison, because `adjacent` is also what gates the segment: one predicate,
    so the copy cannot announce a missing line that the drawing just drew.

    MUTATION that must turn this red: delete the sparse branch (the
    `if (!points.some(...)) { host.append(...) }` block) from renderLatencyChart — the
    one-day case goes back to two unexplained dots.

    WEAK BY CONSTRUCTION: grep-level, like every front-end assertion in this file.
    There is no DOM here, so nothing below renders a chart or counts a dot; it sees
    that the branch and its copy shipped, and that the zero-data branch survived
    alongside it.
    """
    html = client.get("/dashboard").text
    block = _block(html, "charts (SVG)")
    code = _code_only(block)

    # Three distinct states, not two: no runs at all, runs on one day, runs on days.
    assert "!points.length" in code, "the zero-data branch was replaced, not extended"
    assert "!points.some(" in code, "no sparse branch in the latency chart"
    assert "adjacent(points[n - 1], p)" in code, (
        "the sparse branch re-derives adjacency instead of sharing the draw's predicate"
    )
    assert "one day with runs in this window" in code, "the one-day case is unexplained"
    assert "none next to" in code, "days far apart draw no line and say nothing"

    # The caption buys nothing if the fix was to draw the line anyway: the segment is
    # still gated on adjacency, and no unconditional polyline appeared.
    assert "if (adjacent(prev, p)) {" in code, "a segment is no longer gated on adjacency"
    assert "polyline" not in code and "path" not in code.split("renderGauge", 1)[0]
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


# A frame field concatenated straight into a step line: `+ f.reason` or `d.tool +`.
# The lookbehind keeps `head.textContent +` from reading as `d.textContent +`.
_BARE_FRAME_FIELD = re.compile(
    r"\+\s*(?<![\w$.])[fdr]\.\w+|(?<![\w$.])[fdr]\.\w+\s*\+"
)


def test_no_step_describer_interpolates_a_raw_frame_field(client):
    """WR-10: every field these four renderers read is optional, so every one can be null.

    `project()` builds each published frame with `d.get(...)`, so a field is null the
    moment its source event omits the key — and both feed describers concatenated them
    directly. An `error` frame with no reason rendered the literal line `error · null`
    on the public live feed, to every anonymous viewer, on the page whose entire premise
    is that what it shows is real. `renderChunks` had the same shape (`null · null` for
    a malformed result row, which `project_run_detail` tolerates on purpose rather than
    dropping).

    The rule asserted here is mechanical rather than by-example, because the failure is
    per-field: any ONE unwrapped field is the bug, and an example-based test only ever
    covers the fields someone thought of. So this greps each renderer for a frame field
    adjacent to a `+` and requires there to be none.

    MUTATION 1 (executed): unwrap one field — `"error · " + f.reason` in `describe`.
    Reds naming the field it found.

    MUTATION 2 (executed): unwrap `describeOwn`'s `d.cost_usd`. Reds the same way, which
    is the point of scanning all four bodies rather than one.

    WEAK BY CONSTRUCTION: grep over the served source. Nothing here feeds a
    field-missing frame through a describer and reads the rendered line back — there is
    no DOM in this suite. It proves no field REACHES a line unguarded; that dash()
    itself returns the placeholder is pinned by test_the_drill_panel_renders_timings.
    """
    html = client.get("/dashboard").text
    blocks = {
        "feed": _code_only(_block(html, "live feed (/events)")),
        "try": _code_only(_block(html, _TRY)),
        "drill": _code_only(_block(html, _DRILL)),
    }
    renderers = (
        ("describe", "feed", "function describe(f) {"),
        ("runNode", "feed", "function runNode(f) {"),
        ("describeOwn", "try", "function describeOwn(name, d) {"),
        ("renderChunks", "drill", "function renderChunks(results, host) {"),
    )

    for name, where, opener in renderers:
        block = blocks[where]
        assert opener in block, f"{name} is gone — the assertions below would be vacuous"
        body = block.split(opener, 1)[1].split("\n}\n", 1)[0]

        bare = _BARE_FRAME_FIELD.findall(body)
        assert not bare, f"{name} interpolates raw frame fields: {bare}"
        assert "dash(" in body, f"{name} routes nothing through the placeholder helper"
        # ...and nobody "fixed" it by interpolating the word instead of the value.
        # Over the string LITERALS only: `return null;` in describeOwn is a control
        # sentinel meaning "already appended, render no line", not rendered text.
        for literal in re.findall(r'"([^"]*)"', body):
            for word in ("undefined", "null", "None"):
                assert word not in literal, f"{name} renders the word {word!r}"

    # The whole-document rule tests/test_auth.py owns, restated where it can be broken.
    assert "None" not in html


def test_only_the_latest_drill_down_open_may_render(client):
    """WR-07: two opens in flight resolve in ARRIVAL order, not click order.

    Concrete: the visitor clicks run A in the Recent runs table (a slow response —
    /runs/{uid} reads run_events, runs and tickets), then clicks run B in the live feed
    before A comes back. B renders, then A overwrites it. The dialog is now titled with
    A's ticket and, if A is demo-origin, carries the badge reading "You submitted this
    run" — for a run the visitor did not submit and did not ask to see.

    The guard is a monotonic token, and the SHAPE is load-bearing: `openDrill` has
    exactly one `await` and the check is the statement immediately after it, because
    every additional await is another place a render can land ahead of a check. That is
    why all the awaiting lives in `fetchDrill`, which touches no DOM at all.

    MUTATION 1 (executed): delete `if (mine !== drillGeneration) return;`. The
    immediately-after-the-await assertion reds.

    MUTATION 2 (executed): move the fetch back inline — `renderDrill(await
    resp.json())` in openDrill. The one-await assertion reds with 3 awaits, which is
    the structure that made the missing guard possible.

    WEAK BY CONSTRUCTION: there is no DOM and no event loop here, so nothing in this
    test issues two overlapping opens and watches which one wins. It proves the guard
    exists and is positioned where it cannot be bypassed; that it actually suppresses a
    superseded render needs a browser.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _DRILL))

    body = code.split("async function openDrill(uid) {", 1)[1].split("\n}\n", 1)[0]
    assert "const mine = ++drillGeneration;" in body, "the open takes no token"
    assert "let drillGeneration = 0;" in code, "the token counter is not declared"

    # Exactly one await, and the guard is the very next statement after it.
    assert body.count("await ") == 1, (
        "openDrill awaits more than once; every extra await is an unguarded render point"
    )
    assert re.search(r"await [^\n]*\n\s*if \(mine !== drillGeneration\) return;", body), (
        "the staleness check is not the statement immediately after the await"
    )

    # ...and everything that renders is on the far side of it.
    _, after = body.split("if (mine !== drillGeneration) return;", 1)
    for sink in ("drillNotice(", "renderDrill("):
        assert sink in after, f"{sink} can run for a superseded open"

    # The awaiting helper renders nothing, which is what makes the single check above
    # sufficient rather than merely first.
    fetcher = code.split("async function fetchDrill(uid) {", 1)[1].split("\n}\n", 1)[0]
    for sink in ("drillNotice", "renderDrill", "drillFacts", "drillSteps", "drillTitle",
                 "drillEl", "append(", "textContent"):
        assert sink not in fetcher, f"fetchDrill touches the panel with {sink}"


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
    # null/undefined test rather than from falsiness (0 is a real timing). Asserted in
    # the SHARED render-helpers block: dash()/ms() moved there when the feed describers
    # started needing them too (WR-10), and asserting them here would have gone red on
    # the move while the property was intact.
    helpers = _code_only(_block(html, "render helpers"))
    assert helpers.count('(v === null || v === undefined) ? "—"') >= 2
    assert "dash(" in code and "ms(" in code, "the panel stopped using the guards"
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


def test_the_live_feed_can_open_a_drill_down(client):
    """An in-flight run is drillable while it runs, not only after it lands.

    Every feed frame carries `run_uid` (CR-03 stamps it), and GET /runs/{uid} answers
    `status: "in_flight"` for a run whose summary row does not exist yet — so the feed's
    per-run node can open the same panel the table does, and a visitor who watches a run
    start can look inside it immediately.

    This also restates every identifier Phase 5's own test asserts inside that block.
    MUTATION named for that half: drop any one of them (the EventSource constructor, the
    snapshot listener, a FEED_TYPES entry, the run_uid/ticket_id reads, the CLOSED
    branch) — tests/test_run_events.py goes red, which is exactly why they are repeated
    here: this plan ADDS a control to a block a shipped test owns, and must not rewrite
    it. MUTATION for the new half: delete the openDrill control from runNode — the feed
    becomes watch-only again and an in-flight run is undrillable until it finishes.

    WEAK BY CONSTRUCTION: grep over served HTML; no DOM, no click, no dialog.
    """
    html = client.get("/dashboard").text
    feed = _block(html, "live feed (/events)")
    code = _code_only(feed)

    # The new half.
    assert "openDrill(f.run_uid)" in code, "the live feed cannot open a run's trace"

    # The Phase-5 half, unchanged.
    assert 'new EventSource("/events")' in code
    assert 'es.addEventListener("snapshot"' in code
    subscribed = code.split("const FEED_TYPES = [", 1)[1].split("]", 1)[0]
    for frame_type in _STEP_TYPES:
        assert f'"{frame_type}"' in subscribed, f"the page stopped listening for {frame_type}"
    assert "f.run_uid" in code and "f.ticket_id" in code
    assert "EventSource.CLOSED" in code
    assert "setInterval(refresh, 5000)" in html


def test_the_page_never_asks_for_full_fidelity(client):
    """T-06-24 / Pitfall 8: the fidelity decision is server-side, and the page says so.

    Which runs are shown in detail is derived from `tickets.origin`, written by the
    CREATION tier — the route takes one path parameter and there is nothing in its
    signature to tamper with. The actual control is server-side and 06-04's tampering
    test is what proves it. This asserts the weaker, still-worth-having property: the
    page does not even APPEAR to ask, because a page that asks is a page whose next
    author assumes asking works and builds a toggle on top of it.

    MUTATION named for it: append `?full=1` to openDrill's fetch. The server ignores it
    (06-04 pins that by byte comparison), so nothing about the rendered page changes —
    which is precisely why this grep, and not a behavioural test, is the guard here.

    STATED PLAINLY: this test passed the moment it was written; the drill-down never
    had such a parameter. Its whole value is failing the day someone adds one. That is
    the same shape as 06-04's test_create_gate_is_not_charged_twice, and it is named
    rather than dressed up as discovery.
    """
    html = client.get("/dashboard").text
    for token in ("full=", "fidelity=", "X-Demo", "raw=1", "?full", "&full"):
        assert token not in html, f"the page asks the server for more with {token}"
    # No header is set on the drill fetch at all: the route takes a path parameter and
    # nothing else, so a headers bag here could only be an attempt to widen it.
    drill_code = _code_only(_block(html, _DRILL))
    assert "headers" not in drill_code
    assert 'fetch("/runs/" + encodeURIComponent(uid))' in drill_code


# --- Wave 6: the Try-it form (DASH-05) -----------------------------------------------

_TRY = "try it"

_GOLDEN = Path(__file__).parent.parent / "evals" / "golden.jsonl"

# The three cases D-06 asks for — billing, technical/bug, how-to — by their golden ids.
# `evals/golden.jsonl` is DATA and is read here on purpose: the page is supposed to
# carry these cases VERBATIM, and a copy that has drifted from the dataset is a demo
# whose examples are no longer the ones the eval suite proves the agent handles.
# `src/relay/evals.py` is frozen this phase and is not imported.
_TRY_EXAMPLE_IDS = ("refund-monthly", "rate-limits-pro", "password-reset")


def _golden_cases() -> dict:
    cases = {}
    for line in _GOLDEN.read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            cases[case["id"]] = case
    return cases


def _section(html: str, ident: str) -> str:
    """One `<section id="...">` of the served page — MARKUP only, no script.

    The Try-it assertions split in two, and the split is load-bearing: what the markup
    OFFERS (the editable fields, and the absence of an address input) is a different
    question from what the script DOES, and the script names `customer_email` in the
    request body it builds — which is exactly the string the no-address-field assertion
    is about. A grep over the whole document would let one satisfy the other.
    """
    begin = f'<section id="{ident}">'
    assert begin in html, (
        f"the {ident!r} section is gone — every assertion below would be vacuous"
    )
    return html.split(begin, 1)[1].split("</section>", 1)[0]


def test_try_it_offers_three_editable_examples(client):
    """D-06: three prefilled examples — billing, technical, how-to — editable before send.

    The subjects, bodies and customer addresses are asserted against `evals/golden.jsonl`
    itself rather than against string literals repeated here: these three cases are
    already grounded in `kb/` and already cover both terminal actions (an escalation and
    two replies), which is why they were chosen. A page carrying its own paraphrase would
    be demoing something the eval suite does not measure.

    MUTATION that must turn this red: delete one entry from TRY_EXAMPLES (or reword one
    body) — the visitor loses a whole category, and the demo stops covering the
    escalation path that is the drill-down's best moment.

    SECOND MUTATION: render the chosen example as static text instead of into the input
    and textarea — D-06's "editable before submitting" quietly disappears while the page
    still looks complete.

    WEAK BY CONSTRUCTION: there is no DOM in this suite — no jsdom, no headless browser.
    This greps the served HTML and the try-it block; nothing here clicks a chip or reads
    an input's value back. The DOM-level proof is 06-07's human checkpoint.
    """
    html = client.get("/dashboard").text
    section = _section(html, "try-it")
    code = _code_only(_block(html, _TRY))
    cases = _golden_cases()

    for case_id in _TRY_EXAMPLE_IDS:
        case = cases[case_id]
        assert f'"{case["subject"]}"' in code, f"{case_id}'s subject is not on the page"
        assert case["body"] in code, f"{case_id}'s body is not on the page"
        # Pinned to a SEEDED customer: the only rows lookup_customer can find.
        assert f'"{case["customer_email"]}"' in code, (
            f"{case_id} does not pin its seeded customer"
        )

    # Three, and exactly three — a fourth chip pointing at an unseeded address would
    # give the agent a customer lookup that always misses.
    # Counted in the PINNED form (`customer_email: "…"`) so the one dynamic use — the
    # request body's `customer_email: tryEmail` — cannot pad the count.
    assert code.count('customer_email: "') == 3, (
        "the try-it block does not carry exactly three pinned examples"
    )
    for label in ("billing", "technical", "how-to"):
        assert label in code, f"the {label} example is not offered"

    # Editable: the example fills fields the visitor can rewrite before sending (D-06).
    assert '<input id="try-subject"' in section
    assert '<textarea id="try-body"' in section
    assert "trySubject.value = ex.subject" in code
    assert "tryBody.value = ex.body" in code
    # ...and what is sent is what the fields hold, not the example's frozen copy.
    assert "trySubject.value" in code and "tryBody.value" in code


def test_try_it_exposes_no_email_field(client):
    """T-06-27: the visitor cannot type an address, because the address is pinned.

    A demo-origin ticket's drill-down is FULL FIDELITY (D-02) and is retained for the
    sweep window, publicly readable by anyone with the run's uid. An address input would
    therefore let one visitor publish a third party's email address into a 30-day,
    world-readable record — with the site's own form as the publishing mechanism. The
    seeded customers are also the only rows `lookup_customer` can find, so a typed
    address would additionally make the run's first tool call miss.

    MUTATION that must turn this red: add `<input id="try-email" type="email">` to the
    section and read it in the submit path. Everything still works, the run still
    streams, and the threat register's T-06-27 mitigation is gone.

    WEAK BY CONSTRUCTION: grep over the served markup and the try-it code.
    """
    html = client.get("/dashboard").text
    section = _section(html, "try-it")
    code = _code_only(_block(html, _TRY))

    assert "email" not in section, (
        "the try-it markup mentions an address — the address is pinned, not typed"
    )
    assert 'type="email"' not in html
    assert "try-email" not in html
    # The section really does have inputs, so the assertion above is about the ABSENCE
    # of an address field rather than about a section with no fields at all.
    assert "<input " in section and "<textarea " in section

    # The address the request carries comes from the chosen example, server-seeded.
    assert "ex.customer_email" in code, "the address is not taken from the example"
    assert "customer_email: tryEmail" in code


def test_try_it_renders_disabled_without_a_demo_key(client, monkeypatch):
    """Pitfall 14: an unconfigured deployment renders the form disabled, not a broken page.

    `/dashboard` is hit anonymously with NO keys configured by two shipped tests, and it
    is the public landing surface. A setup path that reads the key and wires the submit
    handler unguarded would throw during page setup — and because the script is one
    block, a throw there takes the live feed, the charts and the drill-down with it. A
    naive 200 check would still pass, which is why the disabled BRANCH is asserted in
    the source as well as the response.

    MUTATION that must turn this red: delete the `(not configured)` guard and wire the
    submit handler unconditionally.

    The "None" assertion is tests/test_auth.py's whole-document check, restated because
    this plan adds a block full of new copy and a stray "None yet" label is exactly how
    it goes red.
    """
    monkeypatch.setattr(settings, "demo_key", None)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "(not configured)" in resp.text
    assert '<section id="try-it">' in resp.text, "the form vanished instead of disabling"
    assert "None" not in resp.text

    code = _code_only(_block(resp.text, _TRY))
    assert '"(not configured)"' in code, "nothing on the page notices an unconfigured key"
    assert ".disabled = true" in code, "the form is never disabled"
    # Copy, not silence: a dead button with no explanation reads as a broken page.
    assert "TRY_CONFIGURED" in code


def test_try_it_streams_with_fetch_not_eventsource(client):
    """The run is streamed with `fetch` + a frame-buffered SSE parse, never EventSource.

    `EventSource` takes only `(url, {withCredentials})` — it cannot POST and cannot set a
    header [MDN], so it can neither start a run nor present `X-API-Key`. And the frames
    must be split on a `\\n\\n` BUFFER rather than matched by a regex over the body: SSE
    frames land split across chunk boundaries, and a whole-body regex only produces
    anything once the stream has ended, which is precisely the thing being demonstrated.

    MUTATION that must turn this red: replace the fetch with
    `new EventSource("/tickets/" + id + "/process")`. It is a GET with no key, so the
    perimeter answers 401 and the demo silently never runs — while the page still looks
    like it is trying.

    SECOND MUTATION: buffer the whole body and split it once at the end
    (`(await res.text()).split("\\n\\n")`) — the trace appears all at once when the run
    finishes, which is a transcript, not a live stream.

    WEAK BY CONSTRUCTION: grep over the served page. Nothing here opens a stream or
    parses a frame; the DOM-level proof is 06-07's human checkpoint.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _TRY))

    # The same two calls scripts/demo.sh makes, both carrying the published key.
    assert 'fetch("/tickets"' in code, "the form never creates a ticket"
    assert '"/process"' in code, "the form never runs the ticket it created"
    assert code.count('"X-API-Key": DEMO_KEY') == 2, (
        "both calls must present the published demo key"
    )
    assert code.count('method: "POST"') == 2

    # The streamed read, and the buffer that makes it a stream.
    assert "res.body.getReader()" in code
    assert "TextDecoder" in code and "{ stream: true }" in code
    assert '"\\n\\n"' in code, "frames are not split on a blank-line boundary"
    assert "buffer.indexOf" in code, "there is no frame BUFFER — a split alone loses tails"
    assert 'startsWith("event: ")' in code and 'startsWith("data: ")' in code

    # ...and no EventSource on this path. Exactly one survives on the whole page: the
    # ambient live feed's, which is a GET of a public route and needs no key.
    assert "EventSource" not in code, "the try-it path reached for EventSource"
    assert html.count("new EventSource") == 1
    assert "res.text()" not in code, "the body is read whole instead of streamed"


def test_try_it_deep_links_its_own_run(client):
    """The submitter's run is identified from the SERVER's header and deep-linked.

    Two views of one run are on this page at the same time: the `fetch` stream above is
    the OWNER-facing full-fidelity stream, and the ambient `/events` feed carries the
    REDACTED projection of the same run. Without the uid the page renders one run twice,
    at two fidelities, with nothing connecting them — and cannot open its own trace,
    which is D-02's entire payoff.

    MUTATION that must turn this red: drop the `X-Relay-Run-Uid` read. The stream still
    renders, the feed still shows the run, and the connection between them — plus the
    "see the full trace" control — is silently gone.

    SECOND MUTATION (T-06-30, spoofing): badge the feed from a client-chosen id (a
    `crypto.randomUUID()` or the ticket id) instead of the uid the server returned to
    THIS submitter — the "your run" badge stops meaning anything.

    WEAK BY CONSTRUCTION: grep over the served page; no DOM, no header, no click.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _TRY))

    assert 'res.headers.get("X-Relay-Run-Uid")' in code, (
        "the page never learns which run is its own"
    )
    # The deep link into the drill-down 06-06 shipped — full fidelity, because the
    # ticket was CREATED with the demo key.
    assert "openDrill(uid)" in code, "the visitor cannot open their own run's trace"
    # The badge on the public feed, keyed on the feed's own per-run node map.
    assert "runNodes.get(uid)" in code, "the visitor's run is never badged in the feed"
    assert "your run" in html, "the badge carries no copy"
    # ...and the run stays IN the feed: seeing the same run redacted below and in full
    # above is the security story rendered as an interface.
    assert "remove()" not in code and "delete(" not in code, (
        "the visitor's run is hidden from the public feed instead of badged in it"
    )
    # The identity is the server's, never the client's.
    for invented in ("crypto.randomUUID", "Math.random", "Date.now()"):
        assert invented not in code, f"the run identity is minted client-side with {invented}"


def _fn_body(code: str, opener: str, *, what: str) -> str:
    """One top-level function's body, by its exact opening line.

    The same idiom `test_only_the_latest_drill_down_open_may_render` uses: nested braces
    are indented, so the first `\\n}\\n` is the function's own close. The presence
    assertion comes first because a renamed or deleted function would otherwise make
    every assertion over the body vacuous — which is the exact failure this file is
    trying to stop being.
    """
    assert opener in code, f"{what} is gone — the assertions below would be vacuous"
    return code.split(opener, 1)[1].split("\n}\n", 1)[0]


def test_try_it_controls_are_bound_to_their_handlers(client):
    """DASH-05: the send button, the example chips and the deep link are WIRED.

    WHY THIS EXISTS. The three tests above assert that TOKENS are on the page, and a
    token survives its own wiring being deleted: `openDrill(uid)` sits inside
    `offerTheTrace`'s body, so `test_try_it_deep_links_its_own_run` stays green when
    nothing calls `offerTheTrace` at all. 06-VERIFICATION deleted three bindings
    independently — the send button's, the chips', and the deep link's call site — and
    the suite stayed at 417 green for each. Silent feature loss on the page that is this
    project's call to action.

    So this asserts the CHAIN, link by link, in the order a click travels it:

        trySend --click--> submitTryIt -> runTryIt -> streamRun -> offerTheTrace
                                                                       |
                                                          --click--> openDrill(uid)

    ...plus the chips' binding to chooseExample, and the two POSITIONS that make the
    deep link mean what it says: it is offered only after the run was accepted (past the
    refusal return), and before the read loop, so a stream that drops mid-run still
    leaves the visitor a way into their own trace.

    MUTATION 1 (executed): delete `trySend.addEventListener("click", submitTryIt);`.
    MUTATION 2 (executed): delete `chip.addEventListener("click", () => chooseExample(i));`.
    MUTATION 3 (executed): delete `if (uid) offerTheTrace(uid);`.
    MUTATION 4 (executed): hoist `offerTheTrace(uid)` above the `if (!res.ok)` refusal
    return — a rate-limited visitor is then offered "see the full trace" for a run that
    never started.

    WEAK BY CONSTRUCTION, and this is the honest limit: there is no DOM in this suite
    and adding one was ruled out, so nothing here dispatches a click or observes a
    handler run. It proves the binding and the call site are PRESENT and POSITIONED in
    the shipped source — that they are unremovable without a red — not that a browser
    fires them. That a click actually opens the visitor's own trace is 06-07's
    checkpoint step 5, which remains a human check.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _TRY))

    # --- 1. the send button, and only on a deployment that has a key -----------------
    setup = _fn_body(code, "function setupTryIt() {", what="setupTryIt")
    bind = 'trySend.addEventListener("click", submitTryIt)'
    assert bind in setup, "the send button is bound to nothing — the form cannot submit"
    guard_at, bind_at = setup.index("if (!TRY_CONFIGURED)"), setup.index(bind)
    assert guard_at < bind_at, "the binding is not behind the no-key guard"
    assert "return;" in setup[guard_at:bind_at], (
        "the no-key branch does not return before the binding — a read-only form would"
        " still submit"
    )
    assert re.search(r"^setupTryIt\(\);", code, re.MULTILINE), "setupTryIt is never called"

    # --- 2. the example chips ---------------------------------------------------------
    examples = _fn_body(code, "function renderExamples() {", what="renderExamples")
    assert 'chip.addEventListener("click", () => chooseExample(i))' in examples, (
        "the example chips are inert — D-06's three examples cannot be chosen"
    )
    assert "tryExamplesEl.append(chip)" in examples, (
        "the chip that was bound is not the chip that reaches the page"
    )
    assert "renderExamples();" in setup, "the chips are never rendered"

    # --- 3. the deep link, and where it sits ------------------------------------------
    stream = _fn_body(code, "async function streamRun(ticketId) {", what="streamRun")
    offer_at = stream.find("offerTheTrace(uid)")
    assert offer_at != -1, "streamRun never offers the trace — the deep link is dead code"
    assert stream.index("if (!res.ok)") < offer_at, (
        "the trace is offered before the refusal return — a 429'd visitor would be given"
        " a control for a run that never started"
    )
    assert stream.index('res.headers.get("X-Relay-Run-Uid")') < offer_at, (
        "the trace is offered before the uid the server minted has been read"
    )
    assert offer_at < stream.index("res.body.getReader()"), (
        "the trace is offered only after the read loop — a dropped stream would leave"
        " the visitor with no way into their own run"
    )

    # --- the chain between the click and that call ------------------------------------
    submit = _fn_body(code, "async function submitTryIt() {", what="submitTryIt")
    assert "await runTryIt()" in submit, "the click handler runs nothing"
    run_try = _fn_body(code, "async function runTryIt() {", what="runTryIt")
    assert "await streamRun(ticket.id)" in run_try, "the created ticket is never run"

    # --- and what the offered control does --------------------------------------------
    offer = _fn_body(code, "function offerTheTrace(uid) {", what="offerTheTrace")
    assert 'open.addEventListener("click", () => openDrill(uid))' in offer, (
        "the 'see the full trace' control opens nothing"
    )
    assert "tryActions.append(open)" in offer, "the control is built but never shown"


def test_try_it_renders_refusals_as_designed_states(client):
    """D-08: 429 and 503 render as the cost control working, in the SERVER's own words.

    The rate limiter and the budget ceiling both write product copy into their refusal
    bodies (`detail.note`), and the budget one also computes the reset instant
    (`detail.resets_at`, from `budget_snapshot`). The page renders those strings and
    invents neither. Auth refusals carry a PLAIN STRING detail instead of an object, so
    both shapes are handled — a renderer that only reads `detail.note` shows a blank box
    on the deployment that configured no keys.

    MUTATION that must turn this red: recompute the reset in the browser
    (`new Date().setUTCHours(24, 0, 0, 0)`) instead of rendering `detail.resets_at` —
    a second, disagreeing answer to "when does this reset?" on the one page whose entire
    job is credibility, and rendered in the visitor's local timezone at that.

    SECOND MUTATION: render the refusal through the error path — `alert(...)` or the
    same styling as a failed fetch. It then reads as a fault, which is the exact
    misreading D-08 exists to prevent: the cap is the feature being demonstrated.

    WEAK BY CONSTRUCTION: grep over the served page. The SERVER half of this contract —
    that those fields exist and carry that copy — is asserted for real by
    `test_refusals_render_as_product_copy` below.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _TRY))

    # Branch on the response, on BOTH calls: /tickets can 429 too (demo_create_limit).
    assert code.count("if (!res.ok)") + code.count("if (!created.ok)") >= 2, (
        "a refusal on one of the two calls is unhandled"
    )
    assert "429" in code and "503" in code, "the two refusal statuses are not distinguished"

    # Both detail shapes: an object for the perimeter's refusals, a plain string for auth.
    assert 'typeof detail === "string"' in code, "a string detail renders as blank"
    assert "detail.note" in code, "the server's own copy is not what the page shows"

    # The reset instant is the server's ISO string, rendered verbatim.
    assert "detail.resets_at" in code
    for recompute in ("setUTCHours", "getTimezoneOffset", "toISOString", "86400",
                      "new Date("):
        assert recompute not in code, f"midnight is re-derived in JS with {recompute}"

    # A designed state, not an error toast.
    assert 'class: "refusal"' in code
    assert ".refusal {" in html, "the refusal has no styling of its own"
    for shout in ("alert(", "console.error"):
        assert shout not in code, f"a refusal is reported as a fault with {shout}"


def test_a_dropped_stream_re_enables_the_form_and_stays_distinct_from_a_refusal(client):
    """WR-06: the streaming read is guarded, and its state is not a refusal's state.

    `trySend.disabled = false` used to be written on three branches and reached on a
    fourth that had none: `reader.read()` REJECTS on a mid-stream transport failure, so
    the rejection escaped `submitTryIt` entirely and left "send it" disabled and the
    status line on "working…" for the life of the page. A reload was the only recourse,
    on the page that is this project's call to action and in the failure a
    scale-to-zero demo produces most often.

    The second half matters as much as the first: a 429/503 is a DESIGNED state D-08
    authors server-side, and it must not be swallowed by the new catch and re-rendered
    as "the connection dropped". So this pins that the non-ok branch returns BEFORE the
    guard, which is what keeps the two apart.

    MUTATION 1 (executed): restore the per-branch clears — put `trySend.disabled =
    false` back in `tryFailed()` and `refuse()` and drop the `finally`. The count
    assertion reds with `3 != 1`.

    MUTATION 2 (executed): unwrap the read loop (delete the `try {` before the reader
    and its `catch`). The structural assertion reds with "the streaming read is not
    inside a try block".

    WEAK BY CONSTRUCTION, precisely: there is no DOM in this suite, so nothing here
    drops a connection or reads the button's `disabled` property back. This is a
    structural grep over the served source — it proves the guard and the single
    re-enable site EXIST and are positioned as described, not that a real mid-stream
    reset re-enables a real button. That proof needs a browser.
    """
    html = client.get("/dashboard").text
    code = _code_only(_block(html, _TRY))

    # 1. Exactly one re-enable site on the whole path, and it is a `finally` — the only
    #    construct that runs on the rejecting path as well as the returning ones.
    assert code.count("trySend.disabled = false") == 1, (
        "the button is re-enabled per-branch again; a rejecting path will miss one"
    )
    assert re.search(r"\}\s*finally\s*\{\s*trySend\.disabled = false;\s*\}", code), (
        "the single re-enable is not in a finally"
    )

    # 2. The streaming read is inside a guard, and the guard's catch is in the same
    #    function. Asserted positionally rather than by token presence: a `try` anywhere
    #    else in the block would satisfy a bare `"try {" in code`.
    stream_fn = code.split("async function streamRun(ticketId) {", 1)[1].split("\n}\n", 1)[0]
    head, tail = stream_fn.split("const reader = res.body.getReader();", 1)
    assert head.rstrip().endswith("try {"), (
        "the streaming read is not inside a try block"
    )
    assert "} catch (" in tail, "nothing catches a mid-stream rejection"
    assert "await reader.read()" in tail, "the read moved out of the guarded region"
    # A null body is its own branch, before the guard — `res.body.getReader()` on a
    # bodyless response throws for a reason the copy below would misdescribe.
    assert "if (!res.body)" in head

    # 3. One bad frame is skipped, not fatal: an unguarded JSON.parse inside the loop
    #    would take the whole stream out through the catch above.
    assert re.search(r"try \{ payload = JSON\.parse\(data\); \} catch \(\w+\) \{ continue; \}", code)

    # 4. The refusal path returns BEFORE the guard, so a 429/503 still renders
    #    renderRefusal's designed state and never the dropped-stream copy.
    assert "if (!res.ok) { await refuse(res); return; }" in head
    assert "renderRefusal" not in tail

    # 5. ...and the two states say different things. The dropped-stream copy claims
    #    nothing about why the run stopped and does not clear the steps already shown.
    dropped = code.split("function streamDropped(uid, text) {", 1)[1]
    assert "clear(tryStream)" not in dropped.split("\n}\n", 1)[0], (
        "a transport drop erases steps that really happened"
    )
    assert "dropped mid-run" in code and "may still have finished" in code


def test_refusals_render_as_product_copy(client, monkeypatch):
    """The SERVER half of D-08: every field the page renders is really on the wire.

    The grep test above pins what the page READS. This drives the real routes and pins
    what they SEND — `error`, a non-empty `note`, and for the budget refusal an ISO
    `resets_at` that actually parses. Between them, a rename on either side is caught:
    the page's refusal box would otherwise go quietly blank in production, on the exact
    path a visitor hits when the demo is doing its job.

    No Anthropic call is made or needed: both 429s are raised by the perimeter before
    any run starts (the ticket id below does not exist, and the gate charges its bucket
    before the route body decides that), and the 503 is raised from recorded spend.

    MUTATION that must turn this red on the server side: drop `note` from either refusal
    detail, or return the budget's reset as a bare `"midnight UTC"` string instead of an
    ISO timestamp — the page has nothing to render and no instant to show.
    """
    monkeypatch.setattr(settings, "voyage_api_key", None)
    ticket = {
        "customer_email": "visitor@example.com",
        "subject": "refusals are a designed state",
        "body": "the demo refuses in its own words",
    }

    # 1. POST /tickets — the create allowance (demo_create_limit, 20/hour in production).
    monkeypatch.setattr(settings, "demo_create_limit", "1/hour")
    assert client.post("/tickets", json=ticket, headers=DEMO_HEADERS).status_code == 201
    refused = client.post("/tickets", json=ticket, headers=DEMO_HEADERS)
    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert detail["error"] == "rate_limited"
    assert detail["note"].strip(), "the create refusal carries no copy to render"
    assert detail["retry_after_seconds"] >= 1

    # 2. POST /process — the binding constraint (demo_process_limit, 5/hour).
    monkeypatch.setattr(settings, "demo_process_limit", "1/hour")
    assert client.post("/tickets/9999/process", headers=DEMO_HEADERS).status_code == 404
    refused = client.post("/tickets/9999/process", headers=DEMO_HEADERS)
    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert detail["error"] == "rate_limited"
    assert detail["note"].strip(), "the process refusal carries no copy to render"

    # 3. POST /process — the daily ceiling. Checked BEFORE the tiered window, so it is
    #    what a visitor meets once the demo has spent its day.
    record_run(
        app.state.conn, ticket_id=1, model="m", duration_ms=1, steps=1,
        input_tokens=1, output_tokens=1, cost_usd=settings.max_daily_cost_usd,
        outcome="send_reply",
    )
    refused = client.post("/tickets/9999/process", headers=DEMO_HEADERS)
    assert refused.status_code == 503
    detail = refused.json()["detail"]
    assert detail["error"] == "daily_budget_exhausted"
    assert detail["note"].strip(), "the budget refusal carries no copy to render"
    # An ISO instant the page can print verbatim — never a phrase the browser would
    # have to parse, and never a number it would have to convert.
    assert datetime.fromisoformat(detail["resets_at"]).tzinfo is not None
