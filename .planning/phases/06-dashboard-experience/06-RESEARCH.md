# Phase 6: Dashboard Experience - Research

**Researched:** 2026-08-12
**Domain:** Server-side redaction of a private event log; SQL aggregation for a public metrics API; no-build vanilla-JS/SVG front end over an existing SSE transport
**Confidence:** HIGH (every finding below is traced to source in this repo or produced by a probe run in this session; the two exceptions are tagged `[ASSUMED]` and listed in the Assumptions Log)

## Summary

This phase adds **no new dependency, no new transport, and no new framework.** Everything DASH-02..05 needs is already in the tree: `run_events` holds the full-fidelity record, `project()` is the redaction pattern to extend, `spent_today()` is the budget arithmetic to share, `/process` already streams a run, `/events` already streams the redacted mirror, and hatchling already ships non-`.py` files inside `src/relay/` (verified by building a wheel this session). The phase is therefore almost entirely *composition and disclosure decisions*, not technology selection — which is why this document is heavy on field-by-field allowlists and exact SQL and light on library comparison.

Three findings change the shape of the plan and the planner should read them before anything else:

1. **DASH-03's "timings" is not satisfiable from the current schema.** `run_events.created_at` is `datetime('now')` — second resolution — and `RunRecorder`'s own docstring says so ("there is no tie to break with"). There is no per-event elapsed value anywhere. A guarded `ALTER TABLE run_events ADD COLUMN elapsed_ms INTEGER` stamped by `RunRecorder` is required, and it belongs in Wave 1 with the other migrations.
2. **Try-it cannot correlate its own run.** `run_uid` is minted *inside* `event_stream`'s generator and never reaches the caller of `/process` — not as an event, not as a header. Without it the visitor's run cannot be deep-linked to its own drill-down (the payoff of D-02) and cannot be de-duplicated against the ambient feed. Recommended fix: mint `run_uid` in the handler and return it as a response header on the `StreamingResponse`. This touches zero of the SSE event contract.
3. **The drill-down needs a key that `/metrics` currently refuses to publish.** WR-10 removed `run_uid` from `/metrics` explicitly because "phase 6 has not decided the drill-down's access model". D-01 decides it: public-but-redacted. 05-VERIFICATION routed exactly this to Phase 6 as a human decision. The planner must make the call in the plan, not in passing — recommendation and the alternative are in "Open Questions Q1".

**Primary recommendation:** Build the drill-down redactor as a second, sibling allowlist inside `src/relay/events.py` (never in `main.py`, per WR-04's rule that the redaction boundary is one file a reviewer can open), reusing `_project_tool_result` for the public branch so the drill-down can never disclose more than the feed already does; make the full-fidelity branch its own explicitly-named allowlist rather than a raw spread, so *no* path in the codebase is a `{**payload}`; and anchor the demo exception on the **ticket's creation tier**, not the run's processing tier.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Drill-down disclosure (the phase's security boundary)**
- **D-01:** The per-run drill-down is **public but server-redacted**. A route (`GET /runs/{run_uid}` or similar) returns drill-down JSON built **server-side** through the same allowlist discipline as `project()` — tool inputs/outputs render as shapes and verdicts (tool name, argument keys, result status, retrieval doc ids + scores, cited-vs-not, guardrail denials, timings), never raw text. Timings and the cited-vs-not comparison are not sensitive and render in full.
- **D-02:** **Try-it runs are the full-fidelity exception.** A run created via the dashboard's Try-it form may show raw tool inputs/outputs in its drill-down — the visitor authored that ticket and demo tickets contain no real customer PII. Mechanism for marking a run as demo-originated is planner's choice (e.g. a flag on the run row set when the ticket arrives via the demo key), but the distinction must be made server-side, never by the client.
- **D-03:** This implements Phase 5's W-1 condition (see `src/relay/events.py` `attribute_to_run` docstring): `run_uid` stays a correlation token, never a bearer credential — holding a uid gets you nothing that isn't already redacted. Do not add an authenticated full-fidelity path this phase; if one is wanted later it is its own decision.

**Page architecture**
- **D-04:** `DASHBOARD_HTML` moves out of `main.py` into a **template file** (e.g. `src/relay/templates/dashboard.html`) read once at startup. No build step, no template engine dependency beyond stdlib string substitution as today (`__RELAY_DEMO_KEY__` replacement). This resolves the CLAUDE.md "inline HTML/JS in a Python module" anti-pattern rather than deepening it.
- **D-05:** Single page remains — no route split, no SPA. The drill-down is a client-rendered panel/modal fed by the drill-down JSON route, not a separate HTML page.

**Try-it experience**
- **D-06:** **Three prefilled examples** — billing, bug, how-to — matching the eval categories. Visitor can edit before submitting.
- **D-07:** **Real runs, not dry-run.** The reply/escalation appearing in the drill-down is the payoff; "observably-real" is the core value. The published demo key flows exactly as it does today.
- **D-08:** **Refusals are a first-class UI state, not an error.** When the rate limiter (429) or daily budget (503) refuses, the page renders the actual reason ("demo budget spent for today — resets midnight UTC") as a designed state. The refusal is the cost-control feature being demonstrated.

**Charts & gauge**
- **D-09:** Charts are **client-built inline SVG from `/metrics` JSON** — `/metrics` stays a data API; no server-side SVG rendering.
- **D-10:** Cost/latency over time is **time-bucketed by day** (daily cost total, p50/p95 latency), not per-run points — legible at 3 runs or 300.
- **D-11:** The budget gauge reads **server-computed budget arithmetic**: `/metrics` grows a `budget` object (e.g. `{spent_today_usd, daily_ceiling_usd}`) produced by the same code path as `enforce_daily_budget`. The gauge must never re-derive spend in JS — the gauge and the gate must be incapable of disagreeing.

### Claude's Discretion
- Drill-down route shape, pagination/limits on `run_events` reads, and the demo-run marking mechanism (server-side, per D-02)
- Visual design: layout, typography, color, how cards/charts/feed/drill-down compose on the page
- SVG chart implementation details (axes, scales, empty states)
- How the existing minimal feed UI from Phase 5 is absorbed into the designed experience
- Which `/metrics` additions are needed (outcome distribution buckets, daily buckets) — computed via SQL aggregation per DASH-02, keeping WR-10's explicit-column discipline (never `SELECT *`, never expose `run_uid` on `/metrics`)

### Deferred Ideas (OUT OF SCOPE)
- Authenticated full-fidelity drill-down for non-demo runs (explicitly out per D-03 — its own decision later)
- W-3 (bounding total `/events` connection-holding time) and the tool-name clamp INFO from 05-VERIFICATION — perimeter work, not dashboard work; candidates for a gap-closure pass
- Rejected-action counter, cost-per-stage attribution — v2 (carried from Phase 5)
- Last-Event-ID / SSE resume — Out of Scope (milestone)
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description (from REQUIREMENTS.md) | Research Support |
|----|-------------|------------------|
| DASH-02 | Aggregate cards and outcome distribution (resolved/escalated/error/budget_exceeded/step_limit) render from `/metrics`, computed via SQL aggregation | §"The SQL" — three verified statements replacing `run_metrics`' full-table materialisation; bucket `CASE` maps the exact `outcome` strings `record_run` can write (enumerated from `main.py:293-297,348`) |
| DASH-03 | Per-run drill-down shows the full trace from `run_events`: tool calls with inputs/outputs/timings, retrieval chunks with scores and cited-vs-not highlighting, and guardrail denials | §"Payload-shape map" + §"Drill-down field allowlist" + §"Cited-vs-not". **Timings require a schema addition** — see Pitfall 1 |
| DASH-04 | Cost/latency-over-time renders as hand-rolled inline SVG; a gauge shows remaining daily demo budget — no CDN scripts, no build step | §"Daily buckets" SQL (verified against a percentile oracle), §"budget_snapshot" (D-11), §"Front-end structure" SVG helpers |
| DASH-05 | A "Try it" form with prefilled example tickets submits via the demo key and streams the run live on the page | §"Try-it": `EventSource` cannot POST (verified against MDN) → `fetch` + manual SSE frame parse; three examples lifted from `evals/golden.jsonl`; refusal states enumerated with their exact response bodies |
| DASH-01 *(claim it)* | "The **dashboard receives** a live run feed … (no polling)" | 05-VERIFICATION WARNING-4: DASH-01 is orphaned — Phase 5 shipped the endpoint, no phase owns the browser clause. This phase substantively closes it (the feed UI is absorbed per CONTEXT code_context). **Recommend the planner add DASH-01 to this phase's requirement list** rather than leaving it unowned |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

Directives that constrain this phase's plans. The planner must not produce a task that violates one:

| Directive | Where it bites in Phase 6 |
|-----------|---------------------------|
| Route dependencies, **never** middleware | The drill-down route's rate limit is `Depends(...)`, not middleware |
| All DB reads off the loop via `asyncio.to_thread` | The drill-down read, and the new `/metrics` aggregations |
| Explicit columns, **never** `SELECT *` | `_PUBLIC_RUN_COLUMNS` discipline extends to every new query; `SELECT seq, type, payload, elapsed_ms, ticket_id FROM run_events` |
| No bare `except`; every catch names its type | The only sanctioned broad catch remains `_execute_guarded`'s tool boundary |
| Type hints mandatory; keyword-only past 2-3 args | New functions in `events.py`/`telemetry.py`/`ratelimit.py`. (Note WR-11 is deferred debt in the *existing* `execute_and_record` — do not "fix" it here, it is another phase's line item) |
| Structured logging via `extra={"ctx": {...}}`, dotted event names | Any new log line: `run_detail.swept`, `run_detail.rejected_uid` |
| Domain-specific exceptions; `HTTPException` with explicit status codes | 404 for unknown uid; the rate limiter's dict-detail convention for refusals |
| Module docstrings explain *why*, referencing the phase | New `templates/dashboard.html` has no docstring — put the "why" comment block at the top of its `<script>` and keep the phase-5 `// --- live feed (/events) — begin/end ---` markers (a shipped test greps for them) |
| Named anti-pattern: "Inline HTML/JS in a Python module" | D-04 exists to close this. Do not leave a second HTML string behind in `main.py` |
| Functions returning `(result, is_error)` rather than raising for expected failures | Applies to the drill-down projector's unknown-type handling: return `None` and drop, like `project()` |

---

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Redaction of run events for public display | **API / Backend** (`events.py`) | — | D-01 is explicit: server-side. A client-side filter is not a control. The browser must never receive a field it is expected not to show |
| Deciding a run is demo-originated | **Database + API** (`tickets.origin`, read in the route) | — | D-02: "never by the client". A query param, header, or JS flag is a tampering vector — see Threat T2 |
| Outcome distribution / cards / daily buckets | **Database** (SQL aggregation) | API (shaping) | DASH-02 names SQL aggregation. Today it is a Python loop over every row of `runs` — a full materialisation on a route polled every 5s per tab |
| Daily budget arithmetic | **API** (`ratelimit.spent_today`) | — | D-11: one code path, two consumers (gate + gauge). Any JS arithmetic here re-derives a security control |
| Chart geometry (scales, axes, paths) | **Browser / Client** | — | D-09: `/metrics` stays a data API. Geometry is presentation, not policy |
| Live run rendering | **Browser** (absorbing Phase 5's `EventSource` consumer) | API (`/events`) | Transport is done; this phase is the glass |
| Try-it submission + its own live stream | **Browser** (`fetch` + manual SSE parse) | API (`/tickets`, `/process`) | `EventSource` cannot POST or set `X-API-Key` (verified) |
| Serving the page | **API** (read the packaged template, substitute the key at request time) | Packaging (hatchling/Docker) | D-04. The substitution must stay per-request — a shipped test monkeypatches `settings.demo_key` and requires the page to follow it |

---

## Standard Stack

### Core — everything already in the tree

| Library | Version (verified) | Purpose here | Why standard |
|---------|--------------------|--------------|--------------|
| FastAPI | `>=0.115` (pyproject) | The drill-down route, the extended `/metrics`, serving the template | Already the app |
| Python stdlib `sqlite3` | 3.50.4 local; Debian-bookworm build in the image | Window functions for daily p50/p95 (`ROW_NUMBER() OVER`, since SQLite 3.25) | No extension, no ORM |
| Python stdlib `pathlib` + `html.escape` | 3.11+ | Read the packaged template; escape the demo key | Already the pattern at `main.py:652` |
| hatchling | build backend | Ships `src/relay/templates/dashboard.html` inside the wheel — **verified this session, no pyproject change needed** | Already the backend |
| Browser `fetch` + `ReadableStream` + `TextDecoder` | baseline | Try-it's POST-and-stream (`EventSource` cannot POST) | Platform |
| Browser `document.createElementNS` | baseline | Inline SVG nodes in the SVG namespace | Platform |

### Supporting — nothing to add

**No new runtime dependency is required or recommended.** The milestone constraint is "no build step, no CDN"; every capability above is stdlib or platform.

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `Path.read_text` at import | `importlib.resources.files("relay")` | Correct for zip-imported packages; this app is always an installed dir or an editable checkout, and `Path(__file__).parent` is verified working in both. `importlib.resources` is the more future-proof idiom and costs nothing — either is fine, planner's choice |
| stdlib `.replace()` substitution | Jinja2 | A dependency, a build-ish step, and `${...}` template literals in the inline JS collide with most engines. D-04 forbids it. `.replace()` stays |
| SQL window functions for daily percentiles | Fetch `(day, duration_ms)` rows, group in Python | Python grouping guarantees one percentile definition with `_percentile`; SQL keeps the read O(days) instead of O(runs). **Recommendation: SQL, and change `_percentile` to half-up so the two definitions agree** — verified compatible with the existing `test_percentiles` fixture (see Pitfall 6) |
| `GET /runs/{run_uid}` | `GET /runs/{runs.id}` (the int already public on `/metrics`) | Avoids re-publishing `run_uid`, but the live feed only knows the uid, so a viewer cannot drill into an in-flight run without a second key. See Open Question Q1 |
| `fetch` + manual SSE parse for Try-it | `EventSource("/process")` | Impossible: `EventSource` takes only `(url, {withCredentials})` — no POST, no custom headers, so the `X-API-Key` cannot be sent [CITED: developer.mozilla.org/en-US/docs/Web/API/EventSource/EventSource] |

**Installation:** none.

## Package Legitimacy Audit

**Not applicable — this phase installs no external packages.** No `npm install`, no `pip install`, no CDN `<script>`. The milestone constraint ("no build step, no CDN scripts") and D-09/D-04 forbid it. `slopcheck` was therefore not run; there is nothing for it to check.

If a plan ever proposes adding a package to this phase, that is a scope escape and should be sent back rather than audited.

---

## Architecture Patterns

### System Architecture Diagram

```text
                          ┌────────────────────────── browser (one page, no build) ─────────────────────────┐
                          │                                                                                 │
  visitor ──GET /dashboard┼─▶ template (packaged) ──▶ substituted __RELAY_DEMO_KEY__                         │
                          │                                                                                 │
                          │   ┌── poll 5s ──▶ GET /metrics ──▶ cards │ outcome bars │ 2 SVG charts │ gauge   │
                          │   │                                                                             │
                          │   ├── open ─────▶ GET /events (EventSource, existing) ──▶ live feed (redacted)   │
                          │   │                                                                             │
                          │   ├── click run ▶ GET /runs/{run_uid} ──▶ drill-down panel                       │
                          │   │                                                                             │
                          │   └── Try it ──▶ POST /tickets ──▶ POST /process (fetch stream) ──▶ own trace    │
                          │                        │                    │                                   │
                          └────────────────────────┼────────────────────┼───────────────────────────────────┘
                                                   │                    │  X-Relay-Run-Uid  (NEW)
                        ══════════════════════════ server ══════════════╪═══════════════════════════════════
                                                   │                    │
                        create_gate (tier known) ──┘                    │  full fidelity to the submitter
                        tickets.origin = 'demo'                         │  (unchanged — the caller owns it)
                                                                        ▼
                                                              agent.run_ticket
                                                                        │  yield await _persisted(event)
                                              ┌─────────────────────────┼──────────────────────────┐
                                              ▼                         ▼                          ▼
                                   run_events (RAW payload)     project() ─▶ broker ─▶ /events   runs row
                                   + elapsed_ms (NEW)           (allowlist, unchanged)           (end of run)
                                              │
                                              │  GET /runs/{uid}
                                              ▼
                                   ┌──────────────────────────────────────────────┐
                                   │ events.project_run_detail(rows, full=?)      │  ◀── tickets.origin == 'demo'
                                   │   full=False → public allowlist              │      decided HERE, server-side
                                   │   full=True  → demo allowlist (named fields) │
                                   └──────────────────────────────────────────────┘
```

**The one-sentence invariant:** every byte that leaves the server for an unauthenticated consumer passed through a named field in `events.py`; there is no `{**payload}` on any path, including the demo path.

### Recommended file layout

```
src/relay/
├── templates/
│   └── dashboard.html      # D-04: markup + CSS + vanilla JS, one file, no build
├── events.py               # + project_run_detail() beside project() (WR-04's rule)
├── telemetry.py            # run_metrics() → SQL aggregation; _PUBLIC_RUN_COLUMNS
├── ratelimit.py            # + budget_snapshot(); enforce_daily_budget reads it (D-11)
├── db.py                   # + guarded ALTERs: run_events.elapsed_ms, tickets.origin
└── main.py                 # + GET /runs/{uid}; /metrics composition; template read
tests/
└── test_dashboard.py       # NEW — test_run_events.py is already 2127 lines
```

### Pattern 1: The drill-down redactor lives beside `project()`

`events.py`'s module docstring claims the redaction boundary is one file. WR-04 was raised in Phase 5 precisely because `snapshot_frame` had drifted into `main.py` while that docstring said otherwise, and the fix was to move it back. A drill-down redactor built in `main.py` re-opens that finding on a larger surface.

```python
# src/relay/events.py — beside project(), sharing _project_tool_result

def project_run_detail(
    rows: list[sqlite3.Row], *, full_fidelity: bool, known_tools: frozenset[str]
) -> list[dict]:
    """Redact one run's persisted events into a public drill-down (D-01).

    Takes the WHOLE run rather than one event, because two of the things DASH-03
    asks for are run-level facts, not per-event ones: cited-vs-not is a comparison
    between the accepted send_reply's citations and every search_docs result the
    run saw, and a tool's duration is the gap between its tool_use row and its
    tool_result row.

    `full_fidelity` is D-02's exception and is decided by the CALLER from
    tickets.origin — never from anything the client sent. It selects a SECOND
    allowlist, not a raw spread: even the demo path names `input`, `result` and
    `text` explicitly, so no path in this codebase publishes a field nobody wrote
    down. Default-deny survives the exception.
    """
```

**Reuse, do not re-derive:** the public branch's `tool_result` handling should call the existing `_project_tool_result(d)` (rename it to drop the underscore, or expose a thin public wrapper). This makes it *structurally impossible* for the drill-down to disclose more of a tool result than the live feed already does — the property is enforced by shared code rather than by a reviewer comparing two lists.

### Pattern 2: The demo flag is a property of the ticket, not the run

D-02 offers "a flag on the run row" as an example. Anchor it on the **ticket** instead:

- Full fidelity discloses **ticket content** — the body echoed into `create_escalation.reason`, the email in `lookup_customer.input`, the reply the agent wrote about it. What makes that safe is *who authored the ticket*, which is the **creation** tier.
- A flag on `runs` set from the `/process` tier inverts the safety property: an owner-created ticket carrying real content, processed with the published demo key (which anyone holds), would render full fidelity. That is a leak with a one-line curl.
- The reverse case (demo-created ticket processed by the owner) is harmless: the content is still visitor-authored.

```sql
-- db.py, via the same guarded pattern as run_uid (D-13)
ALTER TABLE tickets ADD COLUMN origin TEXT      -- 'demo' | 'owner' | NULL (legacy)
```

Legacy rows are `NULL` → not demo → redacted. **Fail-closed by default**, which is the same posture `project()` takes for unknown types.

Set it where the tier is known — which today it is not, because every route uses `dependencies=[...]` and discards the gate's return value:

```python
# main.py — module level, because ruff B008 / the repo's own convention
# (main.py:97-98) rejects a call in an argument default
_CREATE_GATE = Depends(create_gate)

@app.post("/tickets", response_model=Ticket, status_code=201)
async def create_ticket(payload: TicketCreate, tier: Tier = _CREATE_GATE) -> Ticket:
    ...
    "INSERT INTO tickets (customer_email, subject, body, origin) VALUES (?, ?, ?, ?)",
    (payload.customer_email, payload.subject, payload.body, tier),
```

`_gate`'s `_dependency` already returns the tier (`main.py:165`); nothing else changes. This touches neither `mcp_server.py` nor `evals.py` (both frozen, and neither creates tickets through this route).

### Pattern 3: One arithmetic, two consumers (D-11)

```python
# ratelimit.py
def budget_snapshot(conn: Database, *, now: float | None = None) -> dict:
    """Today's ceiling as the GATE sees it — the gauge's only source (D-11).

    Includes reserved_usd (runs admitted but not yet in `runs`), because that is
    what enforce_daily_budget compares. A gauge that summed only committed rows
    would read lower than the number that refuses the visitor's next run — a gauge
    and a gate that disagree, which is the one thing D-11 forbids.
    """
    spent = spent_today(conn, now=now)
    ceiling = settings.max_daily_cost_usd
    return {
        "spent_today_usd": round(spent, 4),
        "daily_ceiling_usd": ceiling,
        "remaining_usd": round(max(ceiling - spent, 0.0), 4),
        "exhausted": spent >= ceiling,
        "resets_at": next_utc_midnight().isoformat(),
    }


def enforce_daily_budget(conn: Database) -> None:
    snap = budget_snapshot(conn)
    if not snap["exhausted"]:
        return
    ...
    raise HTTPException(503, detail={
        "error": "daily_budget_exhausted",
        "spent_usd": snap["spent_today_usd"],     # key names UNCHANGED —
        "limit_usd": snap["daily_ceiling_usd"],   # tests/test_ratelimit.py:226-229
        "resets_at": snap["resets_at"],           # asserts all three
        "note": ...,
    }, headers={"Retry-After": ...})
```

The refactor is safe: `spent >= settings.max_daily_cost_usd` is byte-identical semantics, and the three asserted 503 detail keys keep their names.

### Anti-Patterns to Avoid

- **A `?full=1` query param, an `X-Demo` header, or a JS-side "show raw" toggle.** D-02: the distinction is server-side. Any client-reachable switch is a tampering vector and must be covered by a test that sends it and asserts redaction anyway.
- **Building the drill-down projection in `main.py`.** Re-opens WR-04.
- **`{**payload}` anywhere, including the demo branch.** The demo branch is a second allowlist, not an escape hatch.
- **`innerHTML` for anything.** Today's `refresh()` uses it for the cards and the runs table (`main.py:548-556`); the phase-5 test only greps the *feed block*. Rewrite the whole page to `textContent` + `createElement`/`createElementNS` and widen the grep test to the whole template — see Test T6.
- **Re-deriving spend, percentiles, or outcome buckets in JS.** D-11 says it for the gauge; the same logic applies to every number that a server control also computes.
- **Adding a second `/metrics`-shaped endpoint.** D-05 keeps one page; one data route plus one detail route is the whole API surface added.

---

## Payload-shape map (`run_events.payload`, raw)

Traced from every `AgentEvent(...)` construction site in `agent.py` and from `RunRecorder._insert_event` (`events.py:411-425`). `payload` is `json.dumps(event.data, default=str)` — the raw `data` dict, nothing more, nothing less.

| `type` | Raw `payload` keys | Sensitivity | Yield site |
|--------|--------------------|-------------|------------|
| `usage` | `steps`, `input_tokens`, `output_tokens`, `cost_usd`, `max_cost_usd` | all scalars, safe | `agent.py:328` (`RunBudget.snapshot`, `guardrails.py:110-117`) |
| `text` | `text` | **model prose — restates the customer's ticket** | `agent.py:339-341` |
| `tool_use` | `tool`, `input` (raw model-chosen args dict) | **`input` carries email / query / reply body / escalation reason**; `tool` is a model-chosen string | `agent.py:343-345` |
| `tool_result` | `tool`, `result` (parsed tool output), `is_error` | **see per-tool table below** | `agent.py:518-525` and `events.py:474-479` (write tools) |
| `guardrail` (ticket_binding) | `guard`, `tool`, `expected_ticket_id`, `supplied_ticket_id`, `action` | ids are ints; `supplied_ticket_id` is model output | `agent.py:460-469` |
| `guardrail` (citation) | `guard`, `tool`, `missing_citations` (list[str]), `retrieved_ids` (list[str]), `action` | **`missing_citations` is model-authored text**; `retrieved_ids` are KB ids (already public via the feed) | `agent.py:500-509` |
| `notice` | `kind`, `tool`, `retrieval_mode`, `cause`, `results` (an `int` today — WR-02's coercion trap) | scalars, safe **if coerced** | `agent.py:482-493` |
| `resolution` | `via`, plus the whole `budget.snapshot()` | scalars, safe | `agent.py:552-559` |
| `error` | `reason`, plus per-reason extras: `status`+`type` (api_error), `max_steps` (step_limit_reached), budget snapshot (budget_exceeded) | `type` is an **upstream Anthropic-controlled** string (WR-07) | `agent.py:313-324, 331-333, 546-548, 561-568` |

Per-tool `tool_result.result` shapes:

| tool | `result` keys | Sensitivity |
|------|---------------|-------------|
| `lookup_customer` | `found`, `customer{email,name,plan,signed_up}`, `recent_tickets[{id,subject,status,created_at}]` | **whole customer row + 10 ticket subjects — no safe subset** (`events.py:253-255`) |
| `search_docs` | `results[{doc,heading,id,anchors,text,score}]`, `retrieval_mode`, `degraded`, `degraded_cause` | **`text` is retrieved KB prose**; `doc`/`id`/`score` are safe and already on the feed |
| `send_reply` | `reply_id`, `status` | safe |
| `create_escalation` | `escalation_id`, `status` | safe |
| `set_category` | `ticket_id`, `category` | safe |
| any tool, `is_error` | `error` (str), `denied_by`, plus `expected/supplied_ticket_id` or `missing_citations`/`retrieved_ids` | **`error` echoes model output and ticket content** |

**Not in `run_events` at all** (written straight to the caller's SSE stream by `event_stream`, never through the recorder): `error:persistence_failed` (`main.py:352-360`) and the `shutting_down` refusal (`main.py:272-273`). A drill-down of such a run shows the events up to the failure and no terminating row — worth a rendered note rather than a silent truncation.

---

## Drill-down field allowlist

`P` = public branch (`full_fidelity=False`). `D` = demo branch — **additive**, and every added field is named here.

| type | P publishes | D adds | Reasoning |
|------|-------------|--------|-----------|
| *(envelope, every row)* | `seq`, `type`, `elapsed_ms` | — | `seq` is the causal order (`RunRecorder` docstring); `elapsed_ms` is a number. Never `created_at` — second resolution, misleading as a timing |
| `usage` | `steps`, `input_tokens`, `output_tokens`, `cost_usd` | — | identical to `project()` |
| `text` | `char_count` (coerced `int`) | `text` | The feed publishes `{type}` only; a length tells a visitor "the model reasoned for 400 chars" and discloses nothing quotable. Coerced, never `len()` of a non-str |
| `tool_use` | `tool` (clamped to `known_tools`, else `"unknown"`), `arg_keys` (sorted, **intersected with the tool's declared `input_schema.properties`**), `unknown_arg_count` | `input` (raw dict) | D-01 names "argument keys". Raw key names are model-chosen strings — the same class as INFO-1's tool name; intersecting with the declared schema closes it for this new surface without touching `project()` (whose clamp is deferred perimeter work) |
| `tool_result` | **`_project_tool_result(d)` verbatim**, plus per-result `cited: bool` on `search_docs` entries | `result` (raw dict) | Reuse is the control: the drill-down cannot out-disclose the feed by construction |
| `guardrail` | `guard`, `tool`, `action`, `expected_ticket_id` (int-coerced), `supplied_ticket_id` (int-coerced), `missing_count` (int) | `missing_citations` (list[str]) | The prompt-injection story *is* "a ticket body named ticket 999 and the guard denied it" — the ints are the payoff and are ints. `missing_citations` stays out of P: model-authored text |
| `notice` | `kind`, `tool`, `retrieval_mode`, `cause`, `result_count` (coerced) | — | Same coercion trap as WR-02; copy the coercion, not just the field |
| `resolution` | `via`, `cost_usd`, `steps` | — | identical to `project()` |
| `error` | `reason`, `status`, `error_type` | — | identical to `project()`; `error_type` stays renamed for the same collision reason |
| *(unknown type)* | **drop (`None`)** | drop | Same fail-closed default as `project()`: a new yield site is absent until someone adds it here on purpose |

Run-level envelope:

| Field | P | D | Source |
|-------|---|---|--------|
| `run_uid`, `ticket_id` | ✅ | ✅ | the caller already holds the uid; `ticket_id` is public on `/metrics` and `/events` |
| `outcome`, `cost_usd`, `duration_ms`, `steps`, `input_tokens`, `output_tokens`, `model`, `created_at` | ✅ | ✅ | all already in `_PUBLIC_RUN_COLUMNS` |
| `status`: `"complete" \| "in_flight" \| "swept" \| "unrecorded"` | ✅ | ✅ | see the retention/absence matrix below |
| `demo` (bool) | ✅ | ✅ | so the page can label "you submitted this" — the *flag* is not secret, the *content* is |
| ticket `subject`, `body`, `customer_email` | ❌ | `subject` + `body` ✅, `customer_email` ❌ | The visitor's own text is the Try-it payoff. The email is left out even in D: `/tickets` accepts an arbitrary address from anyone holding the published key, and it is the one field a visitor could use to publish a third party's identifier. See Open Question Q3 |

### Absence / retention matrix (the swept-run question)

`purge_expired_run_events` deletes `run_events` at 30 days and **deliberately spares `runs`** (`db.py:250-253`). So four states exist and they must be distinguishable:

| `runs` row | `run_events` rows | Render | HTTP |
|-----------|-------------------|--------|------|
| present | present | `status: "complete"` — the normal case | 200 |
| **absent** | present | `status: "in_flight"` — the row is written in `event_stream`'s `finally` | 200 |
| present | **absent** | `runs.created_at` older than `events_retention_days` → `status: "swept"`, `note: "step detail is kept for 30 days"`; otherwise `status: "unrecorded"` (a legacy pre-Phase-5 run, or 05-VERIFICATION INFO-2's `record_run`-failed case) | 200 |
| absent | absent | unknown run | **404** |

Read `run_events` **before** `runs` in the offloaded callable: a run that completes between the two reads then renders as complete-with-full-events rather than as complete-with-missing-events (all event rows are committed before the `runs` row exists).

### Cited-vs-not

The reply's citations are **not** in the `replies` table — `tools.py:88-90` says so explicitly ("accepted and validated but not persisted this phase"). They exist in exactly one place: the `send_reply` **`tool_use`** row's `payload.input.citations`.

Algorithm, mirroring `_execute_guarded`'s accept-set (`agent.py:151-173`) and `agent.py:400-405`:

1. **Retrieved set per chunk** — for every `search_docs` `tool_result` row with `is_error` false, each `results[i]` licenses `{doc, id, *anchors}` (this is exactly what the loop at `agent.py:400-405` adds to `retrieved_ids`).
2. **Cited set** — from the `send_reply` `tool_use` row whose *following* `send_reply` `tool_result` row has `is_error` false (the **accepted** attempt). A denied attempt's citations are not "cited"; the `guardrail` row already tells that story. No accepted `send_reply` (escalation, error, dry run) → empty set → every chunk renders "retrieved, not cited", which is correct and is itself a legible state.
3. **Compare normalised** — `c.strip().lower()`, both sides, exactly as the guard does (`agent.py:155-157`).
4. A chunk is `cited: true` iff any of its licensed ids is in the normalised cited set.

**Recommendation:** extract the normalisation into one shared helper so the drill-down's highlighting and the guard's accept-set cannot drift. `retrieval.py` already owns `slug()` and the `{doc}#{heading}` id shape, so `retrieval.normalise_citation(s: str) -> str` is its natural home; `agent.py` and `events.py` both call it. If the planner prefers not to touch `agent.py`, duplicate it with a comment naming the other site — but the shared helper is the version a reviewer can verify.

---

## The SQL

Every statement below was **executed this session** against a scratch DB with the real `db.py` schema. Outputs are in the Sources section.

### Outcome distribution (DASH-02)

`outcome` is written in exactly one place (`main.py:385-398`; `grep -rn "record_run(" src/` returns one call site — `evals.py` and `mcp_server.py` never write `runs` rows), from this closed set: `send_reply`, `create_escalation`, `dry_run_complete`, `incomplete`, and `error:{api_connection_error|api_error|model_refusal|budget_exceeded|ended_without_action|step_limit_reached|persistence_failed}`.

```sql
SELECT CASE
         WHEN outcome = 'send_reply'               THEN 'resolved'
         WHEN outcome = 'create_escalation'        THEN 'escalated'
         WHEN outcome = 'dry_run_complete'         THEN 'dry_run'
         WHEN outcome = 'error:budget_exceeded'    THEN 'budget_exceeded'
         WHEN outcome = 'error:step_limit_reached' THEN 'step_limit'
         WHEN outcome LIKE 'error:%'               THEN 'error'
         ELSE 'incomplete'
       END AS bucket,
       COUNT(*) AS n
FROM runs
GROUP BY bucket
ORDER BY n DESC, bucket
```

The two specific error buckets **must precede** the `LIKE 'error:%'` branch — SQLite evaluates `CASE WHEN` in order, so reordering silently collapses `budget_exceeded` and `step_limit` into `error`. That is exactly the mutation Test T3 must red.

Keep the existing raw `outcomes` map in the response as well (a separate `GROUP BY outcome`), or replace it — the planner's call; the existing dashboard reads `m.outcomes` nowhere, so replacing is safe. Adding `outcome_distribution` alongside is the lower-risk move.

### Totals (cards)

```sql
SELECT COUNT(*)                        AS runs,
       COALESCE(SUM(input_tokens), 0)  AS input_tokens,
       COALESCE(SUM(output_tokens), 0) AS output_tokens,
       COALESCE(SUM(cost_usd), 0.0)    AS cost_usd,
       COALESCE(MAX(duration_ms), 0)   AS max_ms
FROM runs
```

Replaces `sum(r[...] for r in rows)` over a full materialisation of `runs` (`telemetry.py:114-133`). `last_runs` keeps its own explicit-column query with `ORDER BY id DESC LIMIT 20` (today it materialises everything and slices `[-20:][::-1]` in Python — an unbounded read on a route polled every 5s per tab).

### Global p50/p95

```sql
WITH ranked AS (SELECT duration_ms, ROW_NUMBER() OVER (ORDER BY duration_ms) AS rn,
                       COUNT(*) OVER () AS n FROM runs)
SELECT MAX(CASE WHEN rn = 1 + MIN(n - 1, CAST(ROUND(? * (n - 1)) AS INTEGER))
                THEN duration_ms END)
FROM ranked
```

### Daily buckets (DASH-04 / D-10)

```sql
WITH windowed AS (
    SELECT date(created_at) AS day, duration_ms, cost_usd,
           ROW_NUMBER() OVER (PARTITION BY date(created_at) ORDER BY duration_ms) AS rn,
           COUNT(*)     OVER (PARTITION BY date(created_at))                      AS n
    FROM runs
    WHERE created_at >= datetime('now', ?, 'start of day')     -- e.g. '-13 days'
)
SELECT day,
       n                              AS runs,
       ROUND(SUM(cost_usd), 4)        AS cost_usd,
       MAX(CASE WHEN rn = 1 + MIN(n - 1, CAST(ROUND(0.50 * (n - 1)) AS INTEGER))
                THEN duration_ms END) AS p50_ms,
       MAX(CASE WHEN rn = 1 + MIN(n - 1, CAST(ROUND(0.95 * (n - 1)) AS INTEGER))
                THEN duration_ms END) AS p95_ms
FROM windowed
GROUP BY day, n
ORDER BY day
```

**Verified this session:**
- Returns `[]` on an empty table (no `NULL` row) — the empty state is a length check, not a null check.
- p50/p95 match a half-up Python oracle for **every** day across a randomised 16-day / 0-6-runs-per-day fixture, and across n = 1..59 × pct ∈ {0.50, 0.95, 0.99} — **0 mismatches**.
- The same comparison against today's banker's-rounding `_percentile` gives **16 mismatches out of 177** — so leaving `_percentile` as-is means the daily chart and the p50 card use two different definitions of "median". See Pitfall 6.
- The `WHERE` is served by `idx_runs_created_at`; `date(created_at)` in `GROUP BY` is a function over the already-pruned rows.
- **Only days with runs are returned.** Densify the series server-side (a `for` loop over the window) so the chart is a plain map and the "no runs on Tuesday" case is not silently a missing point.

### Budget object (D-11)

Not SQL — `budget_snapshot(conn)` (Pattern 3). It calls `spent_today`, which is `DAILY_SPEND_SQL` **plus `reserved_usd()`**. That inclusion is the whole point: it is the number the gate compares. Expect the gauge to jump by up to `max_run_cost_usd` ($0.50) while a run is in flight and settle when the row lands — document that in the UI copy ("includes runs in flight") rather than removing it.

### Composition in the route

```python
@app.get("/metrics")
async def metrics() -> dict:
    def _read() -> dict:
        m = run_metrics(app.state.conn)
        m["budget"] = budget_snapshot(app.state.conn)   # main.py composes; telemetry
        return m                                        # does not import ratelimit
    return await asyncio.to_thread(_read)
```

One offload, one lock acquisition window, module layering intact.

---

## Template extraction & packaging — VERIFIED, not assumed

I built a wheel this session from a copy of this project with `src/relay/templates/dashboard.html` added and **no pyproject change**:

```
relay-0.1.0-py3-none-any.whl
  relay/…
  relay/templates/dashboard.html      ← present
```

Findings:

1. **`[tool.hatch.build.targets.wheel] packages = ["src/relay"]` already ships non-`.py` files.** No `include`, no `artifacts`, no `force-include` needed. [VERIFIED: `pip wheel . --no-deps` + `zipfile.namelist()`]
2. **The Dockerfile needs no change.** It does `COPY src ./src` then `pip install .` (Dockerfile:6-8) — the template rides along inside `src/`. CONTEXT's code_context line "Dockerfile must COPY the templates dir" is already satisfied by the existing wildcard copy; adding a second `COPY` would be dead weight.
3. **`Path(__file__).parent` resolves correctly** in this repo's editable install (`.venv` → `relay.__file__` is the source tree) and in a wheel install (a real directory, not a zip).
4. **Silent-loss risk:** hatchling excludes VCS-ignored files by default. `.gitignore` today has no pattern matching `*.html` or `templates/`, so nothing is excluded — but a future `.gitignore` edit could drop the template from the wheel with **no build error**. The pytest suite runs from the source tree and cannot see this.
5. **The only guard that catches (4) is the CI docker smoke.** `.github/workflows/ci.yml` currently curls `/health` only. **Recommend adding `/dashboard` and `/metrics` to that loop** — two curls that catch a missing template *and* an SQL feature the runtime image's SQLite might not have.

Serving:

```python
# main.py
_TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
# Read once at import (D-04), not per request: /dashboard is the public landing
# surface and this is a syscall per visitor otherwise. Not in lifespan either —
# nothing here binds to a loop, and an import-time failure is the loud, early
# signal that the template did not make it into the image.
DASHBOARD_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8")
```

The `.replace("__RELAY_DEMO_KEY__", escape(...))` **stays at request time** — `tests/test_auth.py:219-227` monkeypatches `settings.demo_key` and requires the served page to follow it. Baking the key at import would red that test (and would ship a stale key on a `fly secrets set`).

---

## Front-end structure (one file, no build)

```
templates/dashboard.html
├── <style>                     plain CSS, grid layout, no framework
├── <body>
│   ├── header                  title + the published-key panel (D-02's disclosure)
│   ├── section#try-it          3 example chips · subject · body · submit · status region
│   ├── section#summary         cards + outcome-distribution bars
│   ├── section#charts          <svg id="cost"> <svg id="latency"> <svg id="gauge">
│   ├── section#feed            the Phase-5 live feed, restyled (markers preserved)
│   ├── table#runs              last_runs — each row a button that opens the drill-down
│   └── <dialog id="drill">     drill-down panel (native <dialog>, no library)
└── <script>                    ~6 named blocks, no module system, no bundler
    ├── const DEMO_KEY = "__RELAY_DEMO_KEY__";
    ├── el() / svg() helpers            createElement / createElementNS
    ├── refresh()                       GET /metrics → cards, bars, charts, gauge
    ├── // --- live feed (/events) — begin/end ---   ← markers kept: a test greps them
    ├── openDrill(uid)                  GET /runs/{uid} → render into <dialog>
    └── tryIt(example)                  POST /tickets → POST /process (streamed)
```

**Rendering rule (one rule, whole file):** every value from the server is written with `textContent`; every node is created with `createElement`/`createElementNS`. No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, or `eval(`. This is a widening of the Phase-5 rule (which is block-scoped) to the whole page, and it is grep-testable — see Test T6.

### Try-it consumes `/process` directly

`EventSource` takes only `(url, {withCredentials})` — no POST, no headers [CITED: MDN]. So Try-it uses `fetch` with a stream reader and a ~15-line SSE frame parser (see Code Examples). Two consequences the planner must design around:

- **Two views of one run.** Try-it's `fetch` stream is the *owner-facing, full-fidelity* stream (`main.py:317-320` — deliberately not the projection). The ambient feed simultaneously shows the *redacted projection* of the same run. Left alone the page renders the run twice, in two fidelities, with no connection between them.
- **Correlation needs the uid** (Finding 2). Recommended fix, smallest possible:

  ```python
  # main.py — mint in the handler instead of inside the generator
  run_uid = uuid.uuid4().hex
  ...
  return StreamingResponse(event_stream(run_uid), media_type="text/event-stream",
                           headers={"X-Relay-Run-Uid": run_uid,
                                    "Cache-Control": "no-cache",
                                    "X-Accel-Buffering": "no"})
  ```

  Same-origin `fetch` reads response headers with no CORS allowlist needed. **Zero change to the SSE event contract** (the milestone's compatibility constraint). Alternative: emit a first `event: run` frame carrying `{run_uid, ticket_id}` — additive and ignorable, but it *is* a contract change and `scripts/demo.sh` would print it.

  The `Cache-Control`/`X-Accel-Buffering` headers are 05-REVIEW's IN-02, unfixed. They cost one line each and they are the difference between "Try-it streams" and "Try-it appears to hang for 20 seconds behind a buffering proxy". Recommend closing IN-02 on both streaming routes as part of this phase.

Once the uid is known the page can: mark the feed's frames for that uid as "your run", and offer "see the full trace" → `openDrill(uid)` (which will be full fidelity, because the ticket was created with the demo key).

### The three examples (D-06)

Lift them verbatim from `evals/golden.jsonl` — they are already grounded in `kb/` and already cover both terminal actions:

| Chip | Golden id | `customer_email` | Body | Expected outcome |
|------|-----------|------------------|------|------------------|
| Billing | `refund-monthly` | `mia@datalane.ai` (pro) | "I was charged for my monthly Pro subscription yesterday but meant to cancel. Can I get a refund?" | **escalation** — `billing.md` says refunds need a human |
| Bug / technical | `rate-limits-pro` | `liam@brightco.io` (pro) | "I'm building an integration and want to know how many requests per minute my plan allows." | **reply with a citation** — `api.md#rate-limits` |
| How-to | `password-reset` | `noah@freetier.dev` (free) | "I forgot my password and can't log in. Can you reset it for me?" | **reply** — `account.md#password-reset` |

Three seeded plans (pro/pro/free — swap one for `ava@acmecorp.com` enterprise if plan variety matters more than category variety), both terminal actions, and every one retrieves a real doc so the cited-vs-not highlighting has something to show on the first click.

**Pin `customer_email` to the seeded address per example and do not expose an email field.** The seeded customers are the only rows `lookup_customer` can find, so a pinned email keeps the demo's full-fidelity drill-down free of any real person's identifier. See Open Question Q3 for the residual.

### Refusal states (D-08)

Every refusal the Try-it path can meet, with the exact body to render (`detail.note` in each case, via `textContent`):

| Where | Status | `detail.error` | Trigger |
|-------|--------|----------------|---------|
| `POST /tickets` | 429 | `rate_limited` | `demo_create_limit` = 20/hour/IP |
| `POST /process` | 429 | `rate_limited` | `demo_process_limit` = **5/hour/IP** — the binding constraint |
| `POST /process` | 503 | `daily_budget_exhausted` | ceiling reached; `detail.resets_at` is an ISO timestamp — render it, do not recompute midnight in JS |
| `POST /process` | 429 | `rate_limited` (tier `outage`) | the ceiling refused *and* the caller is retrying (`outage_process_limit`) |
| `POST /process` | 503 | `shutting_down` | a deploy is draining |
| either | 401 / 503 | plain-string detail | the deployment configured no keys — the page already renders `(not configured)` |
| `GET /events` | 503 | `too_many_viewers` | subscriber cap |
| in-stream | `event: error` | `reason: persistence_failed` | a `run_events` INSERT failed mid-run |

Note the shape difference: rate-limit and budget refusals use **dict** details (`{"detail": {...}}` after FastAPI wrapping); auth refusals use **string** details. The renderer must handle both — `typeof detail === "string" ? detail : detail.note`.

---

## Don't Hand-Roll

| Problem | Don't build | Use instead | Why |
|---------|-------------|-------------|-----|
| Redacting tool results for the drill-down | A second per-tool switch | `events._project_tool_result` (make it public) | Two lists drift; shared code cannot |
| Deciding a citation matched | A fresh string compare | The guard's normalisation (`strip().lower()`, ids ∪ doc ∪ anchors) | The drill-down would show "not cited" for a citation the guard accepted, or vice versa — an audit view that contradicts the control it audits |
| Today's spend for the gauge | A `SUM(cost_usd)` in `telemetry.py` | `ratelimit.spent_today` via `budget_snapshot` | D-11. A parallel SUM omits `reserved_usd` and reads lower than the gate |
| Percentiles | A JS sort in the browser | The verified SQL nearest-rank formula | D-09 keeps `/metrics` a data API; and a JS percentile is a third definition |
| Per-step durations | Diffing `created_at` | A new `elapsed_ms` column | `created_at` is `datetime('now')` — second resolution. `RunRecorder`'s docstring says this outright |
| Escaping model-controlled strings for the page | An HTML escaper in JS | `textContent` | The escaper is the bug; the DOM API has no injection surface |
| SSE parsing for Try-it | A regex over the whole body | A `\n\n`-delimited buffer loop | Frames split across chunk boundaries; a whole-body regex only works after the stream ends, which defeats the point |
| Rate-limiting the new route | A counter | `_gate("run_detail", public=True)` + a new `_LIMIT_SETTINGS` entry | The perimeter is one mechanism |
| A schema migration | `CREATE TABLE IF NOT EXISTS` with the new column | The guarded `PRAGMA table_info` + `ALTER TABLE` pattern (`db.py:276-278`) | D-13: `CREATE TABLE IF NOT EXISTS` **will not add a column** to a table that already exists on the Fly volume. Silent no-op in production only |

**Key insight:** every hand-rolled alternative above creates a *second* definition of something a control already defines. This codebase's recurring defect class is not missing checks — 05-VERIFICATION found 25/25 mutations red — it is two things that can disagree (WARNING-1's `run_uid` docstring, WR-10 vs `attribute_to_run`). The dashboard is a *view of controls*, so every number on it should be produced by the control it displays.

---

## Runtime State Inventory

Included because this phase performs three migrations (two columns and a file move) against a deployment with a live volume.

| Category | Items found | Action required |
|----------|-------------|-----------------|
| **Stored data** | `tickets` on the Fly volume `/data/relay.db` predates `origin` — legacy rows will be `NULL`. `run_events` predates `elapsed_ms` (once Phase 5 deploys) — legacy rows `NULL`. `runs` rows written before Phase 5 have `run_uid IS NULL` and can never be drilled into | Code edit only, **no data migration**: `NULL` origin ⇒ redacted (fail-closed, correct); `NULL elapsed_ms` ⇒ render "—" rather than 0; `NULL run_uid` ⇒ the row's drill-down link is absent, not broken |
| **Live service config** | Fly app `relay-agent`, region `syd`, `min_machines_running=0`, volume `relay_data`. No `[http_service.concurrency]` block (CR-01's proposal was never applied — 05-VERIFICATION WARNING-3) | None this phase. W-3 is explicitly deferred by CONTEXT |
| **OS-registered state** | None — single container, `uvicorn` as PID 1 via `exec` | None |
| **Secrets / env vars** | `RELAY_DEMO_KEY` (published, `relay-demo-2026`), `RELAY_API_KEY`, `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`. New settings this phase (`run_detail_max_events`, `metrics_window_days`, a `run_detail` limit string) are all **defaulted** — nothing new must be set to deploy, matching the Phase-5 config posture | None — add defaults only |
| **Build artifacts / installed packages** | `.venv` is an **editable** install (`relay.__file__` → source tree), so a new `templates/` directory is picked up with no reinstall locally. The Docker image rebuilds from `COPY src` each deploy. `*.egg-info/` is gitignored and not consumed at runtime | None. Verified: `.venv/bin/python -c "import relay; print(relay.__file__)"` → `/…/src/relay/__init__.py` |

**Nothing found in "OS-registered state"** — verified by reading `Dockerfile` (single `CMD`, no scheduler) and `fly.toml` (no `[processes]`, no cron).

---

## Common Pitfalls

**1. DASH-03's "timings" has no source in the current schema.**
`run_events.created_at` is `datetime('now')` — whole seconds — and `RunRecorder`'s docstring says explicitly that `seq` exists *because* there is no sub-second tiebreaker. A tool call that takes 300 ms and one that takes 3 s are indistinguishable. **Avoid:** add `elapsed_ms INTEGER` to `run_events` via the guarded ALTER, stamped by `RunRecorder` from a `time.monotonic()` origin captured in `__init__`. A tool's duration is then `elapsed_ms(tool_result) - elapsed_ms(tool_use)` — and note the write-tool path inserts its `tool_result` row *inside* the tool's transaction after `execute_bound` returns, so that arithmetic is exactly the tool's wall time. **Warning sign:** a plan that renders timings without touching the schema is rendering `created_at` deltas that are almost always `0`.

**2. `CREATE TABLE IF NOT EXISTS` will not add your column in production.**
D-13, learned the hard way in Phase 5 (`db.py:270-278`). It is a silent no-op *only* against the live volume — every local test DB is fresh and green. **Avoid:** guarded `PRAGMA table_info` + `ALTER TABLE`, and follow the existing precedent of **not** also adding the column to the `CREATE TABLE` DDL, so fresh and existing DBs take the same code path and every test exercises the migration. Two new call sites plus the existing one justify a small `_add_column_if_missing(conn, table, column, decl)` helper — which is also where WR-08's deferred race fix will land. **Warning sign:** a test that only ever runs `init_db` on `:memory:`.

**3. `_LIMIT_SETTINGS` is a hard `KeyError`, not a default.**
`_gate("run_detail", public=True)` calls `enforce("run_detail", "anon", request)` → `_limit_item` → `getattr(settings, _LIMIT_SETTINGS[("run_detail","anon")])`. A missing entry is a 500 on the new route, on first request. **Avoid:** add the `("run_detail","anon")` key *and* the settings attribute in the same task. **Do not reuse the `events` bucket** — a drill-down click would then spend the live feed's reconnect allowance and silently break the feed for that visitor.

**4. `"None"` must not appear anywhere in the served page.**
`tests/test_auth.py:230-237` asserts `"None" not in resp.text` with `demo_key` unset. That is a whole-document substring check, and the template is now a big JS file. A comment reading "None yet", a JS `if (x === None)` typo, or a chart label "None" reds it. Lowercase `none` (CSS `display:none`, `border:none`) is fine — the check is case-sensitive. **Warning sign:** the test fails with a message about the demo key while the actual offender is a chart's empty-state label.

**5. Try-it cannot find its own run.**
`run_uid` is minted at `main.py:282`, *inside* the generator, and never leaves the server. Without it: no deep link to the drill-down (D-02's payoff), no de-dupe against the ambient feed, and the page shows the same run twice at two fidelities. **Avoid:** mint it in the handler, return `X-Relay-Run-Uid`. **Warning sign:** a plan whose Try-it task ends at "render the stream" with no drill-down link.

**6. Two definitions of "median".**
`telemetry._percentile` uses Python's `round()` — **banker's rounding**. The SQL nearest-rank formula uses SQLite's `ROUND` — **half-up**. Measured this session: they disagree on **16 of 177** sampled (n, pct) pairs. Ship both and the p50 card and the p50 chart line will visibly differ on some days, on a page whose entire purpose is credibility. **Avoid:** change `_percentile`'s index to `min(n-1, floor(pct*(n-1)+0.5))` and pin the agreement with a property test. **Verified compatible:** the existing `test_percentiles` fixture `[100,200,300,400,1000]` yields the same `p50=300`, `p95=1000` under half-up.

**7. The drill-down is a bigger disclosure surface than the feed, and it is a *back catalogue*.**
`attribute_to_run`'s docstring draws exactly this line: on `/events` the uid is a correlation token for a run the listener is already watching; anywhere else it is a handle to history. This phase makes it a handle to history *on purpose* (D-01), and the safety of that rests entirely on the redaction being complete — including for **error result payloads**, which echo model output and ticket text (`{"error": "citation(s) ['…'] were not retrieved…"}`). `_project_tool_result`'s error branch already drops the message and keeps only `denied_by`; the drill-down must not "improve" on that by showing the error string. **Warning sign:** an `error` or `message` key anywhere in the public branch.

**8. `full_fidelity` must not be reachable from the request.**
Not a query param, not a header, not a cookie, not a client-side toggle. The only input is `tickets.origin`, read server-side. **Warning sign:** the projector taking a `bool` straight off the route signature's default.

**9. `/metrics` is ungated and polled every 5 s per tab.**
It has *no* rate-limit dependency (D-07 keeps it public beside `/health`). The new aggregations must therefore be cheaper than what they replace — they are (SQL aggregation vs materialising every `runs` row, and the daily CTE is index-pruned) — but the daily window must stay bounded by the `WHERE`, and `last_runs` must gain `ORDER BY id DESC LIMIT 20` rather than slicing in Python. **Warning sign:** a query without a `WHERE` or `LIMIT` added to this route.

**10. The projector is a run-level transform, not a per-event one.**
Cited-vs-not needs every `search_docs` result *and* the accepted `send_reply`'s citations; tool durations need row pairs. A `map(project_one, rows)` shape cannot compute either. **Avoid:** `project_run_detail(rows, …)` taking the whole list.

**11. A partially-recorded run must render as itself.**
`error:persistence_failed` and `shutting_down` are never written to `run_events` (they are yielded directly by `event_stream`). Such a run's drill-down ends mid-trace with no terminal row, and its `runs.outcome` says why. Render the outcome; do not let the trace simply stop. Likewise 05-VERIFICATION INFO-2: a failed `record_run` leaves events with **no `runs` row at all** — indistinguishable from in-flight. Both fall out of the absence matrix if it is implemented as specified.

**12. The wheel can silently lose the template.**
Hatchling honours `.gitignore`. Nothing in today's `.gitignore` matches, and the pytest suite reads from the source tree either way — so the *only* signal is a 500 (or an import error) in the built container. **Avoid:** extend the CI docker smoke to `curl -sf /dashboard`.

**13. Don't weaken Phase 5 while restyling the feed.**
`tests/test_run_events.py:2064-2127` greps the page for the `// --- live feed (/events) — begin/end ---` markers, `new EventSource("/events")`, the `snapshot` listener, every name in `FEED_TYPES`, `EventSource.CLOSED`, `f.run_uid`/`f.ticket_id`, `setInterval(refresh, 5000)`, and the absence of markup sinks. A rewrite that drops any of those reds Phase 5's tests. Keep the markers and the identifiers; restyle around them. (If the poll interval changes, that assertion changes with it — a deliberate edit, not a silent one.)

**14. `/dashboard` must survive an unconfigured deployment.**
`test_dashboard_without_a_demo_key_does_not_render_none` and `test_public_routes_need_no_key` both hit it anonymously with no keys set. The Try-it form must render (disabled, with copy) rather than throwing during page setup when `DEMO_KEY` is `"(not configured)"`.

---

## Code Examples

### Guarded column migration (mirrors `db.py:270-278`)

```python
def _add_column_if_missing(conn: Database, table: str, column: str, decl: str) -> None:
    """Idempotent ALTER for a table that already exists on the live volume (D-13).

    CREATE TABLE IF NOT EXISTS declines to touch an existing table, so a column
    added to the DDL above is a silent no-op in production only. ALTER TABLE is
    not idempotent, and init_db runs on every boot — hence the PRAGMA guard.
    (WR-08's check-then-act race is deferred: single-machine, single-writer.)
    """
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

# in init_db, replacing the inline run_uid block:
_add_column_if_missing(conn, "runs", "run_uid", "TEXT")
_add_column_if_missing(conn, "run_events", "elapsed_ms", "INTEGER")
_add_column_if_missing(conn, "tickets", "origin", "TEXT")
```

*(`table`/`column` are literals from this module only — never request-derived — so the f-string is not an injection site. Say so in a comment; a reviewer will look.)*

### `RunRecorder` stamping elapsed

```python
def __init__(self, conn: Database, *, run_uid: str, ticket_id: int) -> None:
    ...
    # Monotonic, per run: the drill-down needs sub-second step timings and
    # created_at is datetime('now') — whole seconds, which is also why seq exists.
    self._t0 = time.monotonic()

def _insert_event(self, event_type: str, data: dict) -> None:
    self._seq += 1
    self.conn.execute(
        "INSERT INTO run_events (run_uid, ticket_id, seq, type, payload, elapsed_ms)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (self.run_uid, self.ticket_id, self._seq, event_type,
         json.dumps(data, default=str), int((time.monotonic() - self._t0) * 1000)),
    )
```

### The drill-down route

```python
_RUN_UID_RE = re.compile(r"\A[0-9a-f]{32}\Z")   # uuid4().hex — main.py:282
run_detail_gate = _gate("run_detail", public=True)


@app.get("/runs/{run_uid}", dependencies=[Depends(run_detail_gate)])
async def run_detail(run_uid: str) -> dict:
    """One run's steps, redacted server-side (D-01).

    Public and keyless, like /events and /metrics, and safe for the same reason:
    content control, not access control. Holding a uid grants nothing that is not
    already redacted (D-03) — except for a demo-originated ticket, where the
    visitor authored the content and D-02 makes the raw trace the payoff. That
    decision is made HERE, from tickets.origin, and is unreachable from the request.
    """
    if not _RUN_UID_RE.match(run_uid):
        # Rejected before the DB is touched: the uid shape is known exactly, and a
        # 404 for a malformed key is cheaper than a 404 for an absent one.
        raise HTTPException(404, "unknown run")

    def _read():
        conn = app.state.conn
        # Events FIRST: they are all committed before the runs row exists, so a run
        # finishing between these two reads renders complete rather than truncated.
        rows = conn.execute(
            "SELECT seq, type, payload, elapsed_ms, ticket_id FROM run_events"
            " WHERE run_uid = ? ORDER BY seq LIMIT ?",
            (run_uid, settings.run_detail_max_events),
        ).fetchall()
        run = conn.execute(
            f"SELECT {', '.join(_DETAIL_RUN_COLUMNS)} FROM runs WHERE run_uid = ?",
            (run_uid,),
        ).fetchone()
        ticket_id = rows[0]["ticket_id"] if rows else (run["ticket_id"] if run else None)
        origin = None
        if ticket_id is not None:
            got = conn.execute(
                "SELECT origin FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            origin = got["origin"] if got else None
        return rows, run, ticket_id, origin

    rows, run, ticket_id, origin = await asyncio.to_thread(_read)
    if run is None and not rows:
        raise HTTPException(404, "unknown run")
    ...
    steps = project_run_detail(
        rows,
        full_fidelity=(origin == "demo"),
        known_tools=frozenset(app.state.registry),
    )
```

### SSE over `fetch` (Try-it)

```javascript
async function streamRun(ticketId, onEvent, onRefusal) {
  const res = await fetch(`/tickets/${ticketId}/process`, {
    method: "POST", headers: { "X-API-Key": DEMO_KEY },
  });
  if (!res.ok) { onRefusal(res.status, await res.json()); return null; }
  const runUid = res.headers.get("X-Relay-Run-Uid");   // same-origin: no CORS expose
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    // Frames are \n\n-delimited and split across chunk boundaries — a regex over
    // the whole body only works once the stream ends, which defeats the point.
    while ((cut = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let name = "message", data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(name, JSON.parse(data));
    }
  }
  return runUid;
}
```

### SVG without a library

```javascript
const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, text) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (text !== undefined) n.textContent = text;   // textContent, always
  return n;
}

// daily cost bars — series is the server-densified array from /metrics
function costChart(series, w = 640, h = 160, pad = 24) {
  const max = Math.max(1e-9, ...series.map(d => d.cost_usd));
  const bw = (w - pad * 2) / Math.max(series.length, 1);
  const g = svg("g", {});
  series.forEach((d, i) => {
    const bh = (h - pad * 2) * (d.cost_usd / max);
    g.append(svg("rect", { x: pad + i * bw + 1, y: h - pad - bh,
                           width: Math.max(bw - 2, 1), height: bh }));
  });
  return g;
}

// budget gauge — a 180° arc, no path library
function arcPath(cx, cy, r, fromDeg, toDeg) {
  const p = (deg) => [cx + r * Math.cos(Math.PI * deg / 180),
                      cy + r * Math.sin(Math.PI * deg / 180)];
  const [x1, y1] = p(fromDeg), [x2, y2] = p(toDeg);
  return `M ${x1} ${y1} A ${r} ${r} 0 ${(toDeg - fromDeg) > 180 ? 1 : 0} 1 ${x2} ${y2}`;
}
// fraction comes straight from /metrics: spent_today_usd / daily_ceiling_usd.
// Never recomputed from last_runs — the gauge and the gate must not disagree (D-11).
```

---

## State of the Art

| Old approach (in this repo today) | Current approach for this phase | Impact |
|---|---|---|
| `run_metrics` materialises every `runs` row and aggregates in Python | SQL aggregation + `LIMIT 20` for `last_runs` | DASH-02's literal requirement; also removes an unbounded read from a route polled every 5 s per tab |
| `DASHBOARD_HTML` as a 124-line string literal in `main.py` | `src/relay/templates/dashboard.html`, read once at import | Closes the CLAUDE.md-named anti-pattern; also removes the file's long-line pressure against ruff's 100-char limit |
| `run_uid` withheld from `/metrics` pending Phase 6's access decision (WR-10) | The access model is decided (D-01/D-03) — see Q1 | Unblocks click-through from the runs table |
| `_percentile` with banker's rounding | One nearest-rank definition shared with SQL | Removes a visible card-vs-chart contradiction before it ships |
| Feed rendering rule scoped to one JS block | Whole-page `textContent`-only rule | The drill-down and Try-it both render model-controlled strings; a block-scoped rule does not cover them |

**Deprecated / superseded by this phase:** nothing external. Everything above is internal debt this phase is positioned to retire.

---

## Assumptions Log

| # | Claim | Section | Risk if wrong |
|---|-------|---------|---------------|
| A1 | The `python:3.12-slim` runtime image bundles SQLite ≥ 3.25, so `ROW_NUMBER() OVER (...)` works there | The SQL / Environment | `/metrics` 500s in production only. Docker is not installed on this machine so I could not confirm the image's `sqlite3.sqlite_version`. Window functions landed in SQLite 3.25 (2018) and Debian bookworm ships 3.40, so the margin is large — but the cheap guard is real: add `curl -sf /metrics` to the CI docker smoke and the assumption becomes a test |
| A2 | `res.headers.get("X-Relay-Run-Uid")` is readable from a same-origin `fetch` with no `Access-Control-Expose-Headers` | Try-it | Only same-origin is in play (the page is served by the same app), so this is standard behaviour; if the dashboard were ever hosted cross-origin the header would need exposing |
| A3 | No plan in this phase needs to modify `evals.py` or `mcp_server.py` | throughout | Both were frozen in Phase 5 and neither creates tickets, writes `runs` rows, or reads the template. Verified by grep (`record_run(` has one call site; `DASHBOARD_HTML` has none outside `main.py`), so this is low risk |

*Everything else in this document is `[VERIFIED]` against this repo's source or by a probe run in this session (see Sources).*

---

## Open Questions (RESOLVED)

> Resolutions (2026-08-12, orchestrator + planner): Q1 — run_uid RESTORED to /metrics
> (plan 06-02; test restated, attribute_to_run docstring corrected in 06-03). Q2 —
> `text` included in the demo branch (06-03/06-06). Q3 — customer_email excluded even
> on the demo branch (06-07). Q4 — "your run" badge rather than feed suppression (06-07).

**Q1 — Does `run_uid` go back onto `/metrics`?** *(the one decision the planner must make explicitly)*
- **What we know:** WR-10 removed it because "phase 6 has not decided the drill-down's access model". 05-VERIFICATION routed the contradiction to Phase 6 as a human decision: *"Either `run_uid` is public (then /metrics may carry it again and the WR-10 test's rationale needs restating) or it is not (then /events must stop stamping it, or Phase 6 must never build an unauthenticated `run_uid`→`run_events` lookup)."* D-01 + D-03 answer it: the lookup **is** unauthenticated and **is** redacted, so a uid grants nothing — for a non-demo run.
- **What's unclear:** demo runs are full fidelity. Publishing uids on `last_runs` turns the demo back-catalogue (30 days) into something anyone can browse, not just whoever was watching the feed at the time.
- **Recommendation:** publish it. Add `run_uid` to `_PUBLIC_RUN_COLUMNS`, restate `test_metrics_does_not_publish_run_uid` as `test_metrics_publishes_exactly_these_columns` (keeping the exact-key-set assertion, which is the mutation guard — only its name and rationale change), and update `attribute_to_run`'s docstring to record that Phase 6 resolved the condition it set. D-02 already accepted that demo content is public; the uid only changes *when* it can be read, and the retention sweep already bounds that to 30 days.
- **Alternative if that feels too wide:** key the route on `runs.id` (already public) and resolve the uid server-side, keeping `run_uid` off `/metrics`. Cost: the live feed only knows the uid, so drilling into an *in-flight* run needs a second lookup path — two keys for one thing, which is the drift this codebase keeps paying for.

**Q2 — Full-fidelity for `text` events?**
- **Known:** `text` is model prose that restates the ticket. For a demo run the ticket is visitor-authored, so the prose is safe by the same argument as the tool inputs, and the model's reasoning is arguably the most interesting thing on the page.
- **Unclear:** it is also the longest, least structured string on the surface.
- **Recommendation:** include it in the demo branch (it is the payoff of "watch it think"), render it with `textContent` inside a collapsed block, and keep `char_count` only in the public branch.

**Q3 — The email field on Try-it.**
- **Known:** `/tickets` requires `customer_email` (`TicketCreate`, `EmailStr`). A demo run's full-fidelity drill-down would show whatever address was submitted, and the published demo key means anyone can `curl` an arbitrary one.
- **Recommendation:** the form does **not** expose an email field — each example pins a seeded customer address. That keeps the dashboard-driven path free of third-party identifiers and makes `lookup_customer` return a real (fake) profile, which is a better demo. Residual: a direct `curl` with the published key can still create a demo-origin ticket carrying an arbitrary address, which then renders full fidelity. If that residual is unacceptable, the narrow fix is to keep `customer_email` redacted **even in the demo branch** (as specified in the allowlist table above) — which is what I recommend, and it costs nothing.

**Q4 — Does the ambient feed suppress the visitor's own run?**
- Suppress, badge, or show both. Once `X-Relay-Run-Uid` exists this is pure UI. **Recommendation:** badge it ("your run") and keep it in the feed — it demonstrates that the public feed is redacted while the visitor's own view is not, which is the security story rendered as an interface.

---

## Environment Availability

| Dependency | Required by | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python venv | everything | ✅ | 3.14.6 (`.venv/bin/python`) | — |
| pytest + pytest-asyncio | the suite | ✅ | pytest 9.1.1, `asyncio_mode=auto` | — |
| ruff | lint gate | ✅ | 0.16.1, `line-length=100` | — |
| SQLite window functions | daily-bucket SQL | ✅ locally | 3.50.4 | see A1 for the container |
| hatchling | packaging the template | ✅ (fetched during the probe build) | current | — |
| Node.js | *not needed* | present (v22.20.0) | — | no build step by constraint; do not introduce one |
| Docker | verifying the built image | ❌ **absent** | — | CI's `docker` job covers it; **extend its smoke to `/dashboard` and `/metrics`** |
| `fly` CLI | post-deploy human checks | ✅ | — | — |
| Anthropic / Voyage APIs | **must not be called** | keys present in `.env` | — | `conftest._no_outbound_http` + per-test `monkeypatch.setattr(settings, "voyage_api_key", None)`; `FakeClient` for the agent loop |

**Missing with no fallback:** none.
**Missing with fallback:** Docker — image-level verification moves to CI.

---

## Validation Architecture

### Test framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.1.1 + pytest-asyncio (`asyncio_mode = "auto"`) |
| Config | `pyproject.toml` `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| Quick run | `.venv/bin/python -m pytest -q tests/test_dashboard.py` |
| Full suite | `.venv/bin/python -m pytest -q` (baseline 341 passed) + `.venv/bin/ruff check src tests` |
| Fixtures available | `client` (TestClient + lifespan + owner key), `conn`, `db`, `registry`, `capture_frames` (drives a real run and returns `(sse_body, published_frames)`), autouse `_reset_limits`, autouse `_no_outbound_http` |
| Doubles | `tests/helpers.py`: `FakeClient`, `TicketAwareFakeClient`, `response`, `tool_use_block`, `text_block`, `usage` |

**New file: `tests/test_dashboard.py`.** `tests/test_run_events.py` is already 2127 lines; Phase 6's tests should not extend it. Phase-5 assertions that live there stay there.

### Requirements → test map

| Req | Behaviour | Type | Command | Exists? |
|-----|-----------|------|---------|---------|
| DASH-03 | **T1** a non-demo run's drill-down leaks no seeded sentinel | integration | `pytest tests/test_dashboard.py::test_run_detail_never_leaks_a_non_demo_runs_content -x` | ❌ Wave 0 |
| DASH-03 | **T2** full fidelity is decided by `tickets.origin`, and no request input can force it | integration | `…::test_full_fidelity_is_server_decided -x` | ❌ |
| DASH-03 | **T9** `cited` matches the citation guard's accept-set | integration | `…::test_cited_vs_not_matches_the_citation_guards_accept_set -x` | ❌ |
| DASH-03 | **T7** a swept run renders as swept, not as missing | unit | `…::test_run_detail_of_a_swept_run_renders_as_swept -x` | ❌ |
| DASH-03 | **T8** the route is rate-limited per IP | integration | `…::test_run_detail_is_rate_limited_per_ip -x` | ❌ |
| DASH-03 | **T14** `elapsed_ms` is stamped and monotonic per run | unit | `…::test_run_events_carry_elapsed_ms -x` | ❌ |
| DASH-02 | **T3** outcome buckets are SQL-computed and map every `outcome` string | unit | `…::test_outcome_distribution_buckets_every_outcome -x` | ❌ |
| DASH-02 | **T10** SQL percentiles == the Python oracle over random data | property | `…::test_daily_percentiles_match_the_oracle -x` | ❌ |
| DASH-04 | **T4** the gauge and the gate cannot disagree | integration | `…::test_budget_gauge_matches_the_gate -x` | ❌ |
| DASH-04 | **T15** the daily series is dense and empty-safe | unit | `…::test_daily_series_is_dense_and_empty_safe -x` | ❌ |
| DASH-04/05 | **T6** no markup sink anywhere in the template | grep | `…::test_dashboard_never_renders_through_a_markup_sink -x` | ❌ (widens an existing block-scoped test) |
| DASH-05 | **T11** `/process` returns the run uid to its submitter | integration | `…::test_process_returns_the_run_uid_to_the_submitter -x` | ❌ |
| DASH-05 | **T16** refusal bodies carry a renderable `note` (429 create, 429 process, 503 budget) | integration | `…::test_refusals_render_as_product_copy -x` | ❌ (429/503 shapes already covered in `test_ratelimit.py`; this asserts the *page-facing* contract) |
| D-04 | **T5** the page is served from the packaged template, key substituted per request | integration | `…::test_dashboard_is_served_from_the_packaged_template -x` | ❌ |
| D-04 | **T12** the built image serves `/dashboard` and `/metrics` | CI smoke | `.github/workflows/ci.yml` docker job | ❌ |
| regression | Phase 5's feed assertions still pass | integration | `pytest tests/test_run_events.py -q` | ✅ exists |

### The load-bearing test — T1, in the Phase-5 idiom

`test_no_projection_leaks_sensitive_data` is the model: **prove presence, then prove absence, per frame and per sentinel, and name the mutation.**

```python
def test_run_detail_never_leaks_a_non_demo_runs_content(client, capture_frames, monkeypatch):
    """D-01, the load-bearing test: an unauthenticated drill-down of a NON-demo run
    discloses no seeded secret, from any of the fields the raw payload carries.

    Presence is proved twice before absence is asserted — once in the run's own
    owner-facing SSE stream and once in the raw run_events payloads — so a run that
    never carried the secrets cannot make this green for the wrong reason.

    MUTATION that must turn this red for ALL sentinels: in project_run_detail, forward
    the raw payload on the public branch — `frame = {**json.loads(row["payload"]),
    "seq": row["seq"]}`. A second, independent mutation that must also red it: default
    `full_fidelity=True`.
    """
    # Sentinels ride the SAME four vectors the raw payload actually carries:
    #   EMAIL  -> lookup_customer.input.email AND its result.customer.email
    #   BODY   -> create_escalation.reason (an OBSERVED field, not just model prose)
    #   KEY    -> search_docs.input.query
    #   CITE   -> a fabricated citation -> guardrail.missing_citations
    ...
    # The ticket is created with the OWNER key, so tickets.origin != 'demo'.
    detail = client.get(f"/runs/{run_uid}").json()
    leaks = [(i, s.get("type"), name)
             for i, s in enumerate(detail["steps"])
             for name, sentinel in SENTINELS
             if sentinel in json.dumps(s)]
    assert leaks == [], f"the drill-down leaked seeded secrets: {leaks}"
    assert sentinel_free(json.dumps(detail))       # envelope too, not just steps
    assert detail["steps"], "an empty drill-down proves nothing"
    assert {"tool_use", "tool_result", "guardrail"} <= {s["type"] for s in detail["steps"]}
```

The last two assertions are what stop the "leaks nothing by publishing nothing" degenerate pass — the same guard `test_events_delivers_a_live_run` uses.

**T2's companion assertion** (tampering, Threat T2): with the same run and `origin` still `NULL`, request `/runs/{uid}?full=1&fidelity=raw` with headers `X-Demo: 1`, `X-Relay-Origin: demo` — the response must be byte-identical to the plain request.

### Sampling rate

- **Per task commit:** `.venv/bin/python -m pytest -q tests/test_dashboard.py` (plus `tests/test_run_events.py` for any task touching the template or `events.py`)
- **Per wave merge:** `.venv/bin/python -m pytest -q && .venv/bin/ruff check src tests`
- **Phase gate:** full suite green (341 + new), ruff clean, and the CI docker job green — the docker job is the only automated check that can see a packaging regression.

### Wave 0 gaps

- [ ] `tests/test_dashboard.py` — the file itself
- [ ] A `seed_runs(conn, *, days, per_day)` helper writing `runs` rows at controlled `created_at` offsets — T3, T10, T15 all need it; today only `test_ratelimit.py` does anything similar and it is inline
- [ ] A `demo_ticket(client)` helper that posts with `X-API-Key: test-demo-key` so `origin == 'demo'` — the counterpart to the existing `_make_ticket`
- [ ] No framework install needed

---

## Security Domain

### Applicable ASVS categories

| ASVS category | Applies | Standard control here |
|---------------|---------|-----------------------|
| V2 Authentication | partial | Unchanged. The new route is deliberately public (D-01/D-03); `create_ticket` starts *using* the tier the gate already resolves, rather than discarding it |
| V3 Session Management | no | No sessions; a published API key |
| V4 Access Control | **yes** | The full-fidelity decision is an authorization decision and must be server-side and non-forgeable: `tickets.origin`, read in the route, never from request input. Default-deny on `NULL` |
| V5 Validation / Encoding | **yes** | `run_uid` validated against `\A[0-9a-f]{32}\Z` before any DB touch; every model-controlled string rendered with `textContent`; tool names clamped to the registry; `arg_keys` intersected with the declared schema; `result_count`/`char_count`/`supplied_ticket_id` coerced, never forwarded (WR-02's lesson) |
| V6 Cryptography | no | Nothing new. `secrets.compare_digest` stays where it is |
| V8 Data Protection | **yes** | The drill-down is a disclosure boundary over a table `db.py` documents as holding raw customer PII. Retention (30 days) already bounds it; the allowlist bounds what leaves |
| V13 API | **yes** | New public JSON route, rate-limited by a route dependency (never middleware), explicit column lists, `LIMIT` on the row read |

### Known threat patterns

| # | Pattern | STRIDE | Mitigation |
|---|---------|--------|------------|
| T1 | Uid harvested from the public feed → drill-down over the back catalogue | Information disclosure | D-01's allowlist; the uid grants nothing extra for non-demo runs (D-03). This is the phase's central control, pinned by T1 |
| T2 | Client forces full fidelity (`?full=1`, `X-Demo`, a JS toggle) | Tampering | Decision derived only from `tickets.origin`; T2's tampering assertion |
| T3 | Owner-created ticket processed with the published demo key to unlock full fidelity | Elevation of privilege | Anchor the flag on the **creation** tier, not the processing tier (Pattern 2). This is why `runs.origin` is the wrong home |
| T4 | Prompt injection makes the model emit an HTML/JS-shaped tool name or argument key that reaches the page | XSS (Tampering) | `textContent` everywhere + tool-name clamp to the registry + `arg_keys` ∩ declared schema. Closes 05-VERIFICATION INFO-1 for this new surface |
| T5 | Error payload echoes ticket content back through the drill-down | Information disclosure | `_project_tool_result`'s error branch already drops the message and keeps only `denied_by` — reuse it, do not re-implement it |
| T6 | Unauthenticated poller drives repeated unbounded reads on `/runs/{uid}` and `/metrics` | DoS | `_gate("run_detail", public=True)` with its own bucket; `LIMIT` on the event read; index-pruned daily window; `LIMIT 20` on `last_runs` |
| T7 | Visitor types real personal data into Try-it, and it becomes publicly readable for 30 days | Information disclosure (consented) | Explicit "this is a public demo" copy on the form; no email field; retention sweep. Residual documented in Q3 |
| T8 | SSE frame splitting via an injected `type` | Tampering | 05-REVIEW IN-03: every `type` is a literal today. The drill-down is JSON, not SSE, so it does not extend this surface — but do not introduce a new SSE writer |

---

## Sources

### Primary (HIGH confidence) — read in full this session

- `src/relay/main.py`, `events.py`, `agent.py`, `telemetry.py`, `db.py`, `ratelimit.py`, `auth.py`, `config.py`, `tools.py`, `models.py`, `guardrails.py` (`RunBudget`/`ToolPolicy`), `retrieval.py` (result shape + `anchors`)
- `tests/conftest.py`, `tests/helpers.py`, `tests/test_observability.py`, `tests/test_auth.py` (dashboard assertions), `tests/test_run_events.py` (sentinel idiom, dashboard grep tests, `/metrics` key-set test)
- `pyproject.toml`, `Dockerfile`, `fly.toml`, `.gitignore`, `.github/workflows/ci.yml`, `scripts/demo.sh`, `kb/*.md`, `evals/golden.jsonl`
- `.planning/phases/06-dashboard-experience/06-CONTEXT.md`; Phase 5's `05-CONTEXT.md`, `05-REVIEW.md`, `05-VERIFICATION.md`, `05-DEFERRED.md`; `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`; `CLAUDE.md`

### Probes run this session (empirical, reproducible)

1. **Wheel build** — copied `pyproject.toml`/`README.md`/`.gitignore`/`src` to a scratch dir, added `src/relay/templates/dashboard.html`, ran `pip wheel . --no-deps`. Result: `relay/templates/dashboard.html` present in the wheel with **no** pyproject change.
2. **Outcome-bucket SQL** — 35 rows across 5 days over the real `db.py` schema: buckets `error 12 / resolved 9 / escalated 4 / step_limit 4 / budget_exceeded 3 / dry_run 2 / incomplete 1`.
3. **Daily-bucket CTE** — verified `[]` on an empty table, and p50/p95 against a Python oracle across a randomised 16-day fixture: **0 mismatches**.
4. **Percentile-definition audit** — n = 1..59 × pct ∈ {0.50, 0.95, 0.99}: SQL vs half-up **0 mismatches**; SQL vs today's banker's-rounding `_percentile` **16 mismatches**. Existing `test_percentiles` fixture unaffected by the half-up change.
5. **Environment probe** — Python 3.14.6, pytest 9.1.1, ruff 0.16.1, SQLite 3.50.4, docker **absent**, node v22.20.0, fly present. `relay.__file__` → source tree (editable install).
6. **Grep audits** — `record_run(` has exactly one call site (`main.py`); `DASHBOARD_HTML` referenced nowhere outside `main.py` and one test docstring; `/dashboard` referenced in 4 test files, `README.md`, `scripts/demo.sh` (comment only).

### Secondary (MEDIUM-HIGH)

- MDN, `EventSource()` constructor — parameters are `(url, {withCredentials})` only; no POST, no custom headers [CITED: developer.mozilla.org/en-US/docs/Web/API/EventSource/EventSource]

### Tertiary (LOW — flagged, not relied upon)

- The runtime image's bundled SQLite version (A1). Not verifiable here; mitigated by a CI smoke addition rather than by assertion.

---

## Metadata

**Confidence breakdown:**
- Standard stack: **HIGH** — nothing new is added; every capability was traced in-tree or verified by probe
- Payload map & allowlist: **HIGH** — enumerated from every `AgentEvent(...)` construction site and every tool executor, cross-checked against `project()`'s existing branches
- SQL: **HIGH** — all four statements executed against the real schema and cross-checked against a Python oracle
- Packaging: **HIGH** — a wheel was actually built and its contents listed
- Front-end: **MEDIUM-HIGH** — the mechanisms (fetch streaming, `createElementNS`, `<dialog>`) are platform baseline and the SSE parse is straightforward; layout and visual design are explicitly Claude's discretion and are not prescribed here
- Demo-marking: **HIGH** on the mechanism, **MEDIUM** on the residual (Q3) — that is a product judgement, not a code fact
- Container SQLite version: **LOW** — A1, mitigated by a CI addition

**Research date:** 2026-08-12
**Valid until:** 2026-09-11 (30 days — this is an internal-architecture phase with a frozen dependency set; the only external fact is MDN's `EventSource` signature, which is stable)
