# Phase 6: Dashboard Experience - Pattern Map

**Mapped:** 2026-08-12
**Files analyzed:** 11 artifacts (5 modified modules, 1 new template, 1 new test module, 4 test-module extensions)
**Analogs found:** 9 / 11 with a concrete in-repo analog; 2 with none (inline SVG, POST-driven SSE consumption)

No RESEARCH.md exists for this phase; every pattern below is taken from the live codebase.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/relay/main.py` — drill-down route `GET /runs/{...}` | route | request-response (read + redact) | `/events` route + `_get_ticket` (`main.py:417`, `main.py:656`) | exact (route shape), role-match (payload) |
| `src/relay/main.py` — `_gate` reuse for the drill-down | middleware (as dependency) | request-response | `events_gate = _gate("events", public=True)` (`main.py:177`) | exact |
| `src/relay/events.py` — stored-event → drill-down redactor | service | transform | `project()` + `_project_tool_result` (`events.py:195-311`) | exact |
| `src/relay/telemetry.py` — `/metrics` outcome distribution + daily buckets | service | batch / aggregate | `run_metrics()` + `_PUBLIC_RUN_COLUMNS` (`telemetry.py:101-141`) | role-match (SQL aggregation is new) |
| `src/relay/ratelimit.py` — shared `budget` object for the gauge | service | transform | `spent_today()` / `enforce_daily_budget()` (`ratelimit.py:155-230`) | exact |
| `src/relay/db.py` — demo-origin column + migration | migration | schema | `run_uid` PRAGMA-guarded ALTER in `init_db` (`db.py:268-285`) | exact |
| `src/relay/db.py` or `main.py` — bounded `run_events` read | model | CRUD (read) | `purge_expired_run_events` (`db.py:240-265`), `_get_ticket` (`main.py:656`) | role-match |
| `src/relay/templates/dashboard.html` | template/view | file-I/O (read once) | `retrieval.load_index()` (`retrieval.py:161-203`) + `build_registry` capture (`tools.py:107-115`) | role-match |
| Dashboard front-end (cards, feed, drill-down, Try-it) | component | request-response + streaming | the marker-delimited feed block (`main.py:560-636`) | exact (feed), partial (rest) |
| Dashboard SVG charts + gauge | component | transform | — | **no analog** |
| `tests/test_dashboard.py` (new) + extensions | test | — | `tests/test_run_events.py:1183`, `:121`, `:1905`, `:2064-2127`; `tests/test_observability.py` | exact |

---

## Pattern Assignments

### 1. `src/relay/main.py` — the drill-down JSON route (D-01, D-03, DASH-03)

**Analogs:** `/events` route (`main.py:417-511`) for the perimeter; `/metrics` (`main.py:410-414`) for the offloaded read; `_get_ticket` (`main.py:656-667`) for the read + 404.

**Gate pattern — copy verbatim** (`main.py:170-177`):
```python
create_gate = _gate("create")
read_gate = _gate("read")
process_gate = _gate("process", meter_spend=True)
# Public (D-11) but not unmetered: /events holds a connection open for its whole idle
# ceiling, which is exactly what a reconnect loop needs to keep the machine awake.
events_gate = _gate("events", public=True)
```
The drill-down is a public read (D-01), so it gets its own bucket:
`drilldown_gate = _gate("runs", public=True)` — which requires a new `("runs", "anon")` entry in
`ratelimit._LIMIT_SETTINGS` (`ratelimit.py:54-71`) and a matching `anon_runs_limit: str` in
`config.Settings` (`config.py:61-68` is the template, comment and all). Reusing `events_gate`'s
bucket would let a drill-down flood consume the live feed's allowance and hide itself in the
feed's log line — the exact reason `("events", "anon")` was split from `("auth", "anon")`.

**Offloaded read pattern — copy** (`main.py:656-667`, and `main.py:410-414`):
```python
row = await asyncio.to_thread(
    lambda: app.state.conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
)
if row is None:
    raise HTTPException(404, "ticket not found")
```
Two things to copy and one to change: keep `fetchone()`/`fetchall()` **inside** the offloaded
callable (the comment above it explains why — `Database` materialises under its lock), and keep
the short-string `HTTPException(404, "…")` form for a domain miss (dict details are reserved for
perimeter refusals — see Shared Patterns). **Change** `SELECT *` to an explicit column list; WR-10
is the discipline `_PUBLIC_RUN_COLUMNS` exists to enforce, and `run_events` has a `payload` column
whose whole point is that it is raw.

**Bound the read.** `run_events` grows ~10 rows/run with no per-run cap other than
`settings.max_agent_steps`; use `ORDER BY seq LIMIT ?` from a new setting, following
`events_retention_days`'s pattern of a defaulted, commented setting (`config.py:138-150`).

**Retention interaction (no analog, must be designed):** `purge_expired_run_events` (`db.py:240`)
deletes rows for runs whose `runs` summary row still exists. A drill-down therefore has three
states, not two: run unknown (404), run known but events swept (200 with an explicit
`events_expired` marker), run known with events (200). Returning 404 for a swept run would make a
30-day-old run indistinguishable from a forged uid.

---

### 2. `src/relay/events.py` — the stored-event redactor (D-01, the phase's security boundary)

**Analog:** `project()` and `_project_tool_result` (`events.py:195-311`). This is an exact analog and
the new function belongs **in this file**, not in `main.py`.

**The allowlist shape — copy branch-for-branch** (`events.py:216-255`):
```python
    tool = d.get("tool")
    result = d.get("result")
    # Coerced, not forwarded: `is_error` is a boolean everywhere it is produced, and a
    # frame whose error flag is None reads as "unknown" to every consumer of the feed.
    is_error = bool(d.get("is_error"))
    if is_error or not isinstance(result, dict):
        # An error, an error string, or a non-JSON result: publish THAT it failed and
        # which guard refused it, never what was refused.
        return {
            "type": "tool_result", "tool": tool, "is_error": is_error,
            "denied_by": result.get("denied_by") if isinstance(result, dict) else None,
        }
    if tool == "search_docs":
        results = result.get("results")
        return {
            "type": "tool_result", "tool": tool, "is_error": False,
            "results": [
                {"doc": r.get("doc"), "id": r.get("id"), "score": r.get("score")}
                for r in results if isinstance(r, dict)
            ] if isinstance(results, list) else [],
        }
```
Four properties to carry over verbatim: (a) failure branch **first**, before any dispatch on tool
name (WR-01 — a denied `send_reply` is a dict and used to take the success branch); (b) the
unrecognised-tool fallthrough keeps only `{tool, is_error}` so a tool added later is redacted by
default; (c) never `{**data}`; (d) coerce unconstrained shapes rather than forwarding them
(`result_count` at `events.py:294-306` is the written-down precedent).

**What differs from `project()`:**
- **Input type.** `project()` takes an `AgentEvent`; this takes a `run_events` row —
  `type` is a column and `payload` is a JSON *string* written with `json.dumps(data, default=str)`
  (`events.py:411-425`). It must `json.loads` defensively: a malformed payload is a dropped step,
  never a raised 500. `load_index`'s "never raises, degrades and logs" posture (`retrieval.py:161`)
  is the model.
- **Richer disclosure, still allowlisted.** D-01 permits argument *keys* (not values), timings,
  doc ids + scores, cited-vs-not, and guardrail denials. Argument keys mean `sorted(input.keys())`
  — a list of key names, never the dict.
- **Cited-vs-not needs two events joined.** `search_docs` results carry `id` (`tools.py:48-70`) and
  `send_reply` carries `citations` (`tools.py:85-98`); the run-scoped `allowed` citation set lives
  in `agent.py:250` and is never persisted. So the comparison is computed by walking this run's
  rows: ids from every `search_docs` tool_result vs the `citations` on the `send_reply` tool_use.
  A `citation` guardrail event (`agent.py:500-505`) carries `missing_citations` — already-public
  shape-wise via the `guardrail` frame's `guard`/`action`, but its `missing_citations` value is
  model output and is deliberately **not** on the live feed; publishing it in the drill-down is a
  new decision that must be made explicitly, not by accident.
- **Timings.** `run_events` has only `created_at` at *second* resolution (`db.py:71-79`), and
  `RunRecorder`'s docstring (`events.py:396-402`) says so outright. Per-step millisecond timings
  DASH-03 asks for do not exist in the table today. Either the payload gains a duration at write
  time (touches `agent.py`/`RunRecorder`, i.e. the phase-5 recorder contract) or the drill-down
  shows only ordering by `seq` plus the run's total `duration_ms` from `runs`. **Flag for the
  planner — this is a requirement/data gap, not a rendering choice.**

**Update the module docstring.** `events.py:1-26` claims exactly two public serialisers, and
`test_events_output_comes_only_from_two_serialisers` (`tests/test_run_events.py:1701`) pins the
`/events` generator to them. A third public serialiser is fine — WR-04 asks that the redaction
boundary be *one file a reviewer can open*, which is why `snapshot_frame` was moved here — but the
docstring must say three, or the file starts lying the way it did before WR-04.

**D-02 (Try-it full fidelity) is a branch, not a second function.** One entry point that takes the
demo flag and chooses redaction; a separate raw path is a second serialisation path, precisely what
`attribute_to_run`'s docstring (`events.py:314-358`) refuses to become.

---

### 3. `src/relay/telemetry.py` — `/metrics` additions (DASH-02, D-10, D-11)

**Analog:** `run_metrics()` + `_PUBLIC_RUN_COLUMNS` (`telemetry.py:101-141`).

**Explicit-column discipline — copy and extend** (`telemetry.py:101-118`):
```python
# The public shape of /metrics, named rather than taken from `SELECT *` (WR-10). ...
_PUBLIC_RUN_COLUMNS = (
    "id", "ticket_id", "model", "duration_ms", "steps",
    "input_tokens", "output_tokens", "cost_usd", "outcome", "created_at",
)

def run_metrics(conn: Database) -> dict[str, Any]:
    rows = [
        dict(r) for r in conn.execute(
            f"SELECT {', '.join(_PUBLIC_RUN_COLUMNS)} FROM runs ORDER BY id"
        ).fetchall()
    ]
```

**What differs:** today every aggregate is computed in Python over *all* rows
(`telemetry.py:119-141`) — the outcome dict is a manual loop, cost is `sum()`. DASH-02 requires SQL
aggregation, and the comment at `main.py:412` ("the one read here that grows unbounded: the
dashboard polls this every 5s") is the standing reason. Move to `GROUP BY` queries:

- outcome distribution → `SELECT outcome, COUNT(*) ... GROUP BY outcome`
- daily buckets (D-10) → `GROUP BY date(created_at)`; there is **no GROUP BY anywhere in this
  codebase today**, so the nearest analog is only the named module-level SQL constant with a
  date predicate, `ratelimit.DAILY_SPEND_SQL` (`ratelimit.py:75-78`):
  ```python
  DAILY_SPEND_SQL = (
      "SELECT COALESCE(SUM(cost_usd), 0.0) FROM runs"
      " WHERE created_at >= datetime('now', 'start of day')"
  )
  ```
  Copy: `COALESCE` so an empty table returns a number not `None`; SQLite's own clock functions so
  the comparison cannot drift with the process timezone (the same reasoning is written out at
  `db.py:259-261`); the constant named at module level so it is greppable.
- daily p50/p95 (D-10): SQLite has no percentile function. Keep `_percentile` (`telemetry.py:94-98`)
  and feed it per-day duration lists — SQL groups, Python percentiles. Say so in a comment; a
  reviewer will otherwise read it as the aggregation DASH-02 asked for being half-done.
- Bound the window. An unbounded `GROUP BY date(...)` over the life of the Fly volume is the same
  growth the current `SELECT` has; a `WHERE created_at >= datetime('now', '-N days')` predicate is
  the fix, and `events_retention_days` (`config.py:138-150`) is the precedent for a defaulted,
  commented window setting.

**Empty state matters.** `test_metrics_empty_state` (`tests/test_observability.py:85`) asserts
`runs == 0` and `p50 == 0` on a fresh DB. Every new key needs a defined empty value — the chart and
gauge render on a demo whose volume was just created.

**`last_runs` still must not carry `run_uid`** — see the open question in *No Analog Found*.

---

### 4. `src/relay/ratelimit.py` — the `budget` object (D-11)

**Analog:** `enforce_daily_budget` / `spent_today` (`ratelimit.py:155-230`) — an exact analog, and
D-11's "the gauge and the gate must be incapable of disagreeing" means it must literally be the
same code path.

**The shape already exists** — it is the 503 detail (`ratelimit.py:216-230`):
```python
    raise HTTPException(
        503,
        detail={
            "error": "daily_budget_exhausted",
            "spent_usd": round(spent, 4),
            "limit_usd": settings.max_daily_cost_usd,
            "resets_at": resets_at.isoformat(),
            ...
```
**Pattern to copy:** extract a `budget_snapshot(conn) -> dict` (keyword-only if it grows past two
args, per the codebase convention) that both `enforce_daily_budget` and `run_metrics`/`/metrics`
call, so the numbers have one producer. `next_utc_midnight()` (`ratelimit.py:150-152`) already
gives `resets_at`.

**Two things to get right, neither of which has an analog:**
1. `spent_today` = `SUM(runs.cost_usd) since UTC midnight` **+ `reserved_usd()`**, the in-flight
   claims (`ratelimit.py:155-163`). The gauge inherits reservations, which is correct under D-11
   (the gate sees them) but means the gauge can move without a run finishing. Document it; do not
   "fix" it by dropping the reservation term, which would be exactly the disagreement D-11 forbids.
2. It touches the DB. `/metrics` already wraps its read in `asyncio.to_thread` (`main.py:410-414`)
   and `_gate`'s `meter_spend` branch explains at length (`main.py:145-156`) why acquiring
   `Database`'s lock on the loop is a measured 0.81s stall against a 3s HEALTHCHECK. One offload
   for the whole `/metrics` payload — do not add a second `to_thread` round trip.

---

### 5. Demo-run marking (D-02) — schema, migration, and the tier plumbing

**Migration analog — copy exactly** (`db.py:268-285`):
```python
def init_db(conn: Database) -> None:
    conn.executescript(SCHEMA)
    # `runs` already exists on the live Fly volume, and CREATE TABLE IF NOT EXISTS does
    # not add columns to a table it declines to create ... So the column is added
    # explicitly. ALTER TABLE is not idempotent ... so the PRAGMA guard is what makes
    # the re-run safe. Legacy rows keep NULL.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "run_uid" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN run_uid TEXT")
```
Any new column on `runs` (or on `tickets`) takes this exact form — add it to `SCHEMA` **and** as a
PRAGMA-guarded ALTER, because the Fly volume has both tables already.

**Optional-keyword propagation analog** (`telemetry.py:57-71`):
```python
def record_run(conn: Database, *, ticket_id: int, ..., outcome: str,
               # Optional so every pre-phase-5 caller (evals, the MCP path, direct test
               # callers) keeps working; legacy rows and un-stamped runs simply store NULL.
               run_uid: str | None = None) -> None:
```
A `demo: bool = False` (or `origin: str | None = None`) rides in the same way — keyword-only,
defaulted, so `evals.py`, `mcp_server.py` and the direct test callers are untouched. The
frozen-caller test (`tests/test_run_events.py:1264`) exists to catch a break here.

**The plumbing gap (concrete, and easy to miss):** the tier is resolved but thrown away. Routes
declare their gate as `dependencies=[Depends(create_gate)]` (`main.py:191-196`, `main.py:216`),
which discards `_gate._dependency`'s `-> Tier | None` return. To mark server-side per D-02 the
route must take it as a parameter instead:
```python
async def process_ticket(ticket_id: int, dry_run: bool = False,
                         tier: Tier | None = Depends(process_gate)) -> StreamingResponse:
```
The `Tier` type and the resolver already exist (`auth.py:22`, `main.py:97`). This is the only
server-side signal available — a header or body field from the client is exactly what D-02
forbids. Note the demo key is *published*, so "demo tier" means "arrived with the public key", not
"came from our form"; if the drill-down's full-fidelity exception must be narrower than that, the
mark has to be minted at ticket creation and carried onto the run, which is a second decision.

---

### 6. `src/relay/templates/dashboard.html` — extraction (D-04)

**Analog for "read a file once at startup, never raise":** `retrieval.load_index`
(`retrieval.py:161-203`) — try/except by named types, a `logger.warning` with `extra={"ctx": {...}}`
carrying `path` and `reason`, and a degraded return rather than a crash. `build_registry`
(`tools.py:107-115`) is the analog for "read once at startup and capture it" rather than per call.

**Keep the substitution exactly as-is** (`main.py:640-653`):
```python
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    """... A .replace() and not an f-string: the inline JS is full of ${...} template
    literals. An unconfigured deployment renders a neutral placeholder ..."""
    published = escape(settings.demo_key) if settings.demo_key else "(not configured)"
    return DASHBOARD_HTML.replace("__RELAY_DEMO_KEY__", published)
```
`escape()` and the `(not configured)` fallback are load-bearing and must survive the move.
Substitution stays **per request** (settings are monkeypatched per test, and `tests/conftest.py:54`
sets `demo_key` inside the fixture); only the file *read* moves to startup.

**Packaging — verify, do not assume.** `06-CONTEXT.md` says "Dockerfile must COPY the templates
dir". That is true for `kb/` because it sits outside the package (`Dockerfile` has a separate
`COPY kb ./kb`), but a template under `src/relay/templates/` is inside
`[tool.hatch.build.targets.wheel] packages = ["src/relay"]` (`pyproject.toml`) and inside
`COPY src ./src`, which runs *before* `pip install .`. So most likely **no Dockerfile change is
needed** — and that is exactly the kind of assumption that ships a container serving a 500 on its
landing page. Pin it with a test that resolves the template through the installed package
(`Path(relay.__file__).parent / "templates" / "dashboard.html"`), not through the repo root.

Load path should be `Path(__file__).parent / "templates" / ...`, **not** a `settings` path:
`db_path` and `kb_dir` are deployment-configurable data; the template is package code.

---

### 7. Dashboard front-end (DASH-02/03/04/05)

**Analog:** the marker-delimited live-feed block (`main.py:560-636`) and its two grep tests.

**The block-marker + textContent discipline — extend it** (`main.py:560-566`):
```javascript
// --- live feed (/events) — begin ---
// Everything below renders frames off the PUBLIC feed. They are allowlisted by
// project(), but `tool` is a model-chosen string that reaches this page verbatim,
// so every value from a frame is written with textContent and never as markup.
// That is the one rule this block may not break — a test greps this block for the
// markup sinks and fails if one appears.
```
```javascript
function onFrame(ev) {
  const f = JSON.parse(ev.data);
  const line = document.createElement("div");
  line.className = "step";
  line.textContent = describe(f);
  runNode(f).append(line);
}
```
**The landmine:** the *other* half of the current page renders through `innerHTML` with template
literals (`main.py:548`, `main.py:553`) over `/metrics` values, which are numbers and enumerated
outcome strings. The drill-down panel renders **tool names, argument keys and doc ids** — all
model-influenced, and `INFO-1` in `05-VERIFICATION.md` names the tool name as the one unbounded
model-controlled string on the surface. So: the drill-down and Try-it blocks get their own
begin/end markers and their own grep test, or the whole `<script>` becomes the asserted region.
Do not let a new block inherit the polling half's `innerHTML` habit by proximity.

**Reuse, do not rewrite, the feed consumer** (`main.py:615-635`): `new EventSource("/events")`, the
`snapshot` listener, `FEED_TYPES.forEach(t => es.addEventListener(t, onFrame))` (every frame carries
an `event:` name, so a `message`-only listener receives nothing), grouping by `f.run_uid`, and the
`EventSource.CLOSED` branch that treats an idle close as normal rather than as a fault.
`test_the_dashboard_subscribes_to_the_live_feed` (`tests/test_run_events.py:2076`) asserts each of
those, so a rewrite that drops one turns the suite red.

**Refusal states (D-08).** The copy already exists server-side and the page should render
`detail.note` rather than invent its own: `rate_limited` (`ratelimit.py:128-147`),
`daily_budget_exhausted` ("the cap is a feature, not a fault", `ratelimit.py:216-230`),
`too_many_viewers` (`main.py:439-449`), `shutting_down` (`main.py:103-106`). All four are dict
details with `error` + `note` + `Retry-After` — one client-side handler covers them.

**Try-it (D-05/D-07):** `scripts/demo.sh:25-37` is the closest analog for the sequence —
`POST /tickets` with `X-API-Key`, then `POST /tickets/{id}/process` streamed. `EventSource` cannot
issue a POST, so the page needs `fetch` + a `ReadableStream` reader and a hand-rolled SSE split, or
it must submit and then watch its own run arrive on `/events`. **No analog exists for either**; see
below.

---

## Shared Patterns

### Perimeter: route dependencies, never middleware
**Source:** `main.py:109-167` (`_gate`), `auth.py:1-9`, `ratelimit.py:9-12`
**Apply to:** every new route
A `StreamingResponse` locks its status at 200 on first yield, so a rejection raised any later than
the dependency can only surface as an in-stream error on a 200. The anon bucket is charged *before*
the credential resolves; `public=True` is anon-meter + route bucket and no tier.

### Every DB touch is offloaded
**Source:** `main.py:410-414`, `main.py:656-667`, `main.py:54-56`, `main.py:145-156`
**Apply to:** the drill-down read, the `/metrics` aggregation, the budget snapshot
```python
return await asyncio.to_thread(run_metrics, app.state.conn)
```
`fetchone()`/`fetchall()` go **inside** the offloaded callable. One offload per route, not one per
query — the cost is acquiring `Database`'s lock, not running the statement.

### Explicit columns, never `SELECT *`
**Source:** `telemetry.py:101-118` (`_PUBLIC_RUN_COLUMNS`), and the test that pins it
**Apply to:** every new query on `runs` and `run_events`
The star is how `run_uid` became public the moment it was added. Name the columns; the next one
someone adds is then a decision, not a silent public API change.

### Keyword-only arguments past the second
**Source:** `telemetry.py:57` (`record_run`), `db.py:240` (`purge_expired_run_events`),
`events.py:405` (`RunRecorder.__init__`), `events.py:314` (`attribute_to_run`)
**Apply to:** every new helper. New optional parameters are keyword-only **and defaulted**, so
existing callers (`evals.py`, `mcp_server.py`, direct test callers) stay untouched.

### Structured logging
**Source:** `main.py:57-59`, `events.py:159-168`, `ratelimit.py:122-125`
**Apply to:** every new log site
```python
logger.info("run_events.retention_swept", extra={"ctx": {
    "deleted": purged, "retention_days": settings.events_retention_days,
}})
```
Dotted event name as the message; context in `extra={"ctx": {...}}`, never interpolated. Per-module
logger named `relay.<module>`. Never log a payload value from `run_events`.

### Error-response convention (two forms, deliberately)
**Source:** `main.py:666` vs `ratelimit.py:128-147`
**Apply to:** the drill-down route
Domain misses are short strings: `raise HTTPException(404, "ticket not found")`. Perimeter refusals
are dict details with `error` / `note` / `Retry-After`, because D-08 requires them to read as
product copy. No bare `except`; catch by named type only.

### Settings carry their reasoning
**Source:** `config.py:61-68`, `config.py:116-150`
**Apply to:** every new setting (drill-down page size, metrics window, `anon_runs_limit`)
Defaulted so nothing must be configured to deploy, with a comment stating what breaks at the wrong
value.

### Tests name the mutation that turns them red
**Source:** `tests/test_run_events.py:61-67`, `:121-134`, `:1183-1194`, `:1905-1916`, `:2076-2084`
**Apply to:** every load-bearing control in this phase
```python
def test_metrics_does_not_publish_run_uid(client):
    """WR-10: the join key into the PII table is not on the public endpoint.
    MUTATION that must turn this red: restore `SELECT * FROM runs` in run_metrics — the
    uid reappears in last_runs and both assertions fail. The exact key set is asserted
    rather than just the uid's absence, so the NEXT column somebody adds to `runs` is a
    decision here rather than a silent public API change."""
```
Also copy the **anti-vacuity half**: `test_no_projection_leaks_sensitive_data`
(`tests/test_run_events.py:1224-1248`) first asserts the sentinels *did* reach the run's own stream
and the raw `run_events` rows, then asserts their absence from published output. An absence test
without it stays green against a run that never carried the secret.

### The leak test, adapted for the drill-down
**Source:** `tests/test_run_events.py:1183-1261`
**Apply to:** the drill-down route and its redactor
Three sentinels enter through three fields the redactor actually inspects (`EMAIL_SENTINEL` via
`lookup_customer.email`, `KEY_SENTINEL` via `search_docs.query`, `BODY_SENTINEL` via
`create_escalation.reason`), then:
```python
    leaks = [
        (i, frame.get("type"), frame.get("tool"), name)
        for i, frame in enumerate(frames)
        for name, sentinel in SENTINELS
        if sentinel in json.dumps(frame)
    ]
    assert leaks == [], f"published frames leaked seeded secrets: {leaks}"
```
Per-item, not against one concatenated blob, so the failure names which item and which secret.
Add the D-02 inverse: the same run marked demo-originated **does** return the sentinels — otherwise
"full-fidelity for Try-it runs" is untested and can silently regress to redacted-for-everyone.

### Public-route perimeter test
**Source:** `tests/test_run_events.py:1905-1934` (rate limit), `:1937-1970` (capacity refusal)
**Apply to:** the drill-down route
```python
    monkeypatch.setattr(settings, "anon_events_limit", "1/minute")
    first = client.get("/events")
    second = client.get("/events")
    assert second.status_code == 429, "a second connect from the same IP was not metered"
    assert second.json()["detail"]["error"] == "rate_limited"
```
Mutation named in the docstring: drop `dependencies=[Depends(...)]` from the decorator.

### Dashboard grep tests
**Source:** `tests/test_run_events.py:2054-2127`
**Apply to:** the drill-down panel, the charts, the Try-it form
Copy the `_feed_block` marker-extraction helper and the "markers gone → every assertion below is
vacuous" guard, plus the sink list:
```python
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in block, f"the live feed renders frame values through {sink}"
```
Keep the honest header comment at `:2054-2062` — there is no DOM in this suite, so these are
regression guards on served HTML, not evidence a browser renders anything.

### Test fixtures to reuse rather than re-invent
**Source:** `tests/conftest.py`
`client` (TestClient with the owner key on default headers, tmp DB, `demo_key="test-demo-key"`),
`conn`/`db`, `capture_frames` (drives a real run and returns `(body, frames)`), and the autouse
`_reset_limits` / `_no_outbound_http`. A Try-it test that posts with the *demo* key must set the
header explicitly, as `tests/test_ratelimit.py` does.

---

## No Analog Found

| Artifact | Role | Data Flow | Reason / what to do instead |
|----------|------|-----------|------------------------------|
| Inline SVG charts + budget gauge | component | transform | Nothing in this repo draws SVG or builds DOM beyond the feed's `createElement`/`textContent`. Only transferable rule: build nodes with `document.createElementNS("http://www.w3.org/2000/svg", ...)` and set text with `textContent` — the existing `innerHTML` habit at `main.py:548`/`553` must not spread here. Tests can only grep for the sink list and for an empty-state branch. |
| SQL `GROUP BY` daily/outcome aggregation | service | batch | No `GROUP BY` exists anywhere in the codebase. Nearest pattern is `ratelimit.DAILY_SPEND_SQL`'s named constant + `COALESCE` + SQLite clock functions. Percentiles stay in Python (`telemetry._percentile`). |
| POST-driven live stream consumption (Try-it) | component | streaming | `EventSource` cannot POST, and the page has never issued an authenticated POST. `scripts/demo.sh:25-37` is the shape of the two calls; the browser-side `fetch` + `ReadableStream` SSE parse has no in-repo precedent. Alternative with a real analog: submit, then let the existing `/events` consumer render the run — but that path is redacted, and D-07 says the reply appearing is the payoff. |
| Per-step millisecond timings | model | — | `run_events` stores only `created_at` at second resolution (`db.py:71-79`, `events.py:396-402`). DASH-03's "timings" cannot be satisfied from the table as it stands. |

### Open question the planner must resolve first (not a pattern — a contradiction)

The drill-down is keyed on `run_uid` (D-01/D-03), but `/metrics` deliberately does **not** publish
`run_uid` — `_PUBLIC_RUN_COLUMNS` (`telemetry.py:101-110`), `test_metrics_does_not_publish_run_uid`
(`tests/test_run_events.py:121-157`), and the `attribute_to_run` docstring (`events.py:332-353`)
all pin that position. So "click a run in the table" has no handle today. Three routes:

1. **Publish `run_uid` on `/metrics`.** D-03 ("holding a uid gets you nothing that isn't already
   redacted") is exactly the condition `attribute_to_run`'s docstring set for allowing this — but
   taking it means deliberately rewriting that test and that docstring, which are written as the
   guard against this happening by accident. If chosen, do it loudly.
2. **Key the drill-down on `runs.id`,** which `/metrics` already publishes, resolving id → uid
   server-side. Touches no phase-5 decision. Cost: a run clicked from the *live feed* carries only
   `run_uid`, so the route needs to accept both, or the feed's runs stay unclickable until their
   summary row exists.
3. **Only feed-observed runs are clickable.** Smallest surface, worst experience — the table is
   history, and history is what a visitor scrolls.

Whichever is chosen, `test_metrics_does_not_publish_run_uid`'s exact-key-set assertion
(`tests/test_run_events.py:145-148`) will fail the moment `/metrics` grows the outcome-distribution
and budget keys at the top level or a column in `last_runs`; that failure is the design working —
update it deliberately, per-key.

---

## Metadata

**Analog search scope:** `src/relay/` (all 20 modules), `tests/` (conftest, test_run_events,
test_observability, test_ratelimit index), `pyproject.toml`, `Dockerfile`, `scripts/demo.sh`,
`.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`
**Files scanned:** 14 read in full or in targeted ranges; 34 enumerated
**Pattern extraction date:** 2026-08-12
