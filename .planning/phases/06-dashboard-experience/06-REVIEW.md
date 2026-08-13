---
phase: 06-dashboard-experience
reviewed: 2026-08-13T10:05:00Z
depth: deep
files_reviewed: 12
files_reviewed_list:
  - src/relay/events.py
  - src/relay/main.py
  - src/relay/telemetry.py
  - src/relay/db.py
  - src/relay/ratelimit.py
  - src/relay/config.py
  - src/relay/tools.py
  - src/relay/templates/dashboard.html
  - tests/test_dashboard.py
  - tests/test_metrics.py
  - .github/workflows/ci.yml
  - .gitignore
findings:
  critical: 2
  warning: 10
  info: 11
  total: 23
status: issues_found
---

# Phase 6: Dashboard Experience — Code Review Report

**Reviewed:** 2026-08-13
**Depth:** deep (cross-file: the disclosure boundary end-to-end, the /metrics query plans, the front-end read as code, test falsifiability by executed mutation)
**Scope:** `git diff f43e644...HEAD` — Phase 6 (PR #8) plus `quick/dashboard-sparse-states`
**Baseline at review start AND finish:** `407 passed`, `ruff check src tests` clean, working tree clean. Only this file was added. Every mutation run during the review was reverted and the suite re-run to 407.

---

## Summary

The **public** (redacted) half of the disclosure boundary is genuinely closed, and the tests that pin it are among the strongest in the repository — I traced every reachable path and could not open it. The defect is on the other side: the **demo/full-fidelity branch discloses data the visitor did not author and could not have authored**, on a keyless route, for 30 days. Specifically, `lookup_customer`'s raw input and raw result are republished, which carries a third party's email address plus up to ten of *that person's* ticket subjects — the exact `lookup_customer` payload `06-RESEARCH.md:328` classifies as "whole customer row + 10 ticket subjects — **no safe subset**". The phase's own threat table (`06-04-PLAN.md:215`, T-06-16) claims this is mitigated and `06-03-SUMMARY.md:88` claims `customer_email` is "excluded on BOTH branches". Neither is true in code, and the shipped test that certifies it is shaped so it cannot notice.

Elsewhere: the drill-down's carefully-argued private rate-limit bucket is unreachable (the shared anon bucket is half its size and is charged first); `/metrics` remains the one route with no perimeter at all and this phase made its per-call cost heavier, not lighter, contrary to the comment above the queries; and the front-end's whole test surface — five grep tests plus the CI docker smoke — stays green with the page's single rendering statement deleted.

The `/metrics` daily window **is** genuinely bounded in SQL (`telemetry.py:227`), and `test_the_window_bounds_the_chart_not_the_ledger` asserts the query and its `EXPLAIN QUERY PLAN` directly rather than through `run_metrics` — that is a correctly-built test and the concern raised in the brief is closed. The half-up percentile agreement holds: I confirmed SQLite's `ROUND` is half-away-from-zero (`0.5→1, 2.5→3`), which equals `floor(x+0.5)` for the non-negative ranks these queries produce.

### Verdict 1 — can a **non-demo** run's raw content be reached by ANY path?

**No.** Traced exhaustively:

- `run_detail` (`main.py:504`) takes exactly one path parameter. No query param, header or cookie is read; `test_full_fidelity_is_server_decided` byte-compares a tampered request against a plain one and I re-confirmed the signature by hand.
- `full_fidelity` is keyword-only with no default (`events.py:339`), so no caller can drift into it.
- `demo = origin == "demo"` (`main.py:583`), equality not truthiness, so `NULL` (every pre-migration row) fails closed.
- `tickets.origin` is written in exactly one place: `create_ticket` (`main.py:250`), from the tier the *creation* gate resolved. `mcp_server.py:45` and `evals.py:339` insert without the column → `NULL`. No `UPDATE` anywhere touches `origin` (only `status` and `category`, `tools.py:79,96,103`). So a run's fidelity cannot be flipped after the fact.
- `_RUN_UID_RE` is `\A[0-9a-f]{32}\Z` — `\Z`, not `$`, so no trailing-newline smuggling. The rejection log truncates to 12 chars and `JsonFormatter` json-escapes, so no log injection.
- The public branch of `project_run_detail` publishes **no payload value** — I re-ran it against a real `lookup_customer` payload and got `{seq, type, elapsed_ms, tool, arg_keys, unknown_arg_count}` / `{seq, type, elapsed_ms, tool, is_error, duration_ms}` and nothing else.

**But the question's second half fails.** The demo branch — which is reachable by anyone, because the key it turns on is published on the page by design — discloses content that is *not* the submitter's. See CR-01. So: no non-demo **run** is reachable at full fidelity, and yet a non-demo **person's** data is.

### Verdict 2 — is restoring `run_uid` to `/metrics` safe?

**Conditionally, and the condition is not currently met.**

`attribute_to_run`'s docstring (`events.py:567-596`) states the test correctly: the uid may return to `/metrics` only while "holding a uid grants nothing that is not already redacted", and it names its own kill switch — *"If a future change makes a uid grant anything a caller did not already have — an unredacted field … that is the moment it becomes a bearer credential, and this is the line to delete."*

For a redacted (non-demo) run the reasoning holds and I verified it: the uid is already broadcast to every anonymous `/events` listener via `attribute_to_run`, and the drill-down it opens publishes strictly less than the live feed already did. Withholding it on `/metrics` was never protection.

For a **demo-origin** run the kill switch is already tripped: the uid grants raw tool inputs, raw tool results, model prose and the ticket body — including a third party's identifier (CR-01). `/metrics.last_runs` now hands out the 20 most recent uids to anyone, keylessly, so an attacker does not even need to have kept the `X-Relay-Run-Uid` header from their own `POST /process`. Fix CR-01 and the restoration is sound as argued; leave CR-01 and the uid is a bearer credential over exactly the content the phase promised it would never be.

---

## Critical Issues

### CR-01: the full-fidelity branch republishes a third party's email address and ten of that person's ticket subjects, on a keyless public route, for 30 days

**Severity:** CRITICAL / BLOCKER — information disclosure of personal data belonging to someone other than the requester
**Files:** `src/relay/events.py:470-471, 481-482, 514-515` (the `if full_fidelity:` sites) · `src/relay/main.py:567-574, 583, 613-623, 627-629` · `src/relay/tools.py:33-43` (`lookup_customer`)

**Issue.** D-02's premise is *"the visitor authored that ticket and demo tickets contain no real customer PII"*. That premise does not survive contact with the agent loop: the very first thing the system prompt tells the model to do is call `lookup_customer` (`tools.py:118-121` — *"Call this first for every ticket"*), and `lookup_customer` returns

```json
{"found": true,
 "customer": {"email": ..., "name": ..., "plan": ..., "signed_up": ...},
 "recent_tickets": [{"id":…, "subject": …, "status":…, "created_at":…}, × 10]}
```

On the demo branch `project_run_detail` publishes that **raw**:

- `events.py:481-482` — `step["input"] = raw_input` → the address the ticket named, verbatim.
- `events.py:514-515` — `step["result"] = payload.get("result")` → the whole customer row **and** up to ten of that customer's ticket subjects.

Those subjects are not the visitor's. On the deployed service they are whatever the *owner* key has been filing against that address — real support tickets from a real person.

**Concrete failure scenario (no credential the page does not already hand out):**

```bash
K=relay-demo-2026                       # published in <code> on /dashboard, by design
ID=$(curl -s -XPOST $H/tickets -H "X-API-Key: $K" -H 'content-type: application/json' \
     -d '{"customer_email":"ava@acmecorp.com","subject":"account check",
          "body":"can you look at my account history?"}' | jq .id)
UID=$(curl -sD- -o/dev/null -XPOST $H/tickets/$ID/process -H "X-API-Key: $K" \
      | grep -i x-relay-run-uid | cut -d' ' -f2)      # or just read /metrics.last_runs
curl -s $H/runs/$UID                                   # NO KEY AT ALL
```

The response carries `"demo": true` and, inside it, `ava@acmecorp.com`, her name, her plan, and her last ten ticket subjects. `run_detail`'s docstring (`main.py:517-519`) states the intent precisely — *"`customer_email` is withheld even on the demo branch: /tickets accepts an arbitrary address from anyone holding the published key, so it is the one field a visitor could use to publish a third party's identifier"* — and then withholds it from exactly one of the three places it appears.

**This was foreseen and the recommended fix was not applied.** `06-RESEARCH.md:904` (Q3): *"a direct `curl` with the published key can still create a demo-origin ticket carrying an arbitrary address, which then renders full fidelity. … the narrow fix is to keep `customer_email` redacted **even in the demo branch** — which is what I recommend, and it costs nothing."* `06-RESEARCH.md:985` names both vectors by name (`lookup_customer.input.email` AND `result.customer.email`). `06-04-PLAN.md:215` then records T-06-16 as *mitigated*. The mitigation landed only on `main.py:629`'s `ticket` envelope — the `tickets.customer_email` **column** — while the two vectors RESEARCH enumerated were left open.

**The containment claim is also narrower than stated.** `project_run_detail`'s docstring (`events.py:368-372`) argues that reusing `_project_tool_result` makes *"the drill-down can never show more of a tool result than /events already does"* a **structural** property. It is structural on the public branch only: `events.py:514-515` assigns `step["result"]` from the raw payload *after* `step.update(detail)`, bypassing `_project_tool_result` entirely. On the demo branch the drill-down shows strictly more than `/events` ever does, including the whole `lookup_customer` row the fall-through at `events.py:264-266` exists to destroy.

**Proof (executed, then reverted):** driving the shipped route with a demo-origin ticket whose own address is the one the agent looks up — i.e. the production shape the Try-it form actually produces — the keyless `GET /runs/{uid}` response contained

```
"tool":"lookup_customer", …, "result":{"found":true,"customer":{"email":"drill-sentinel-…@example.com",
 "name":"Drill Sentinel","plan":"enterprise",…},"recent_tickets":[{"id":1,"subject":"API limits",…}]}
```

**Fix.** Do not hand the raw `result`/`input` out per-tool; allowlist the demo branch per tool the way the public branch already is, and put `lookup_customer` on neither side (it has no safe subset, by RESEARCH's own finding):

```python
# events.py — the demo branch is a SECOND allowlist, so it needs the same per-tool dispatch
_DEMO_RAW_TOOLS = frozenset({"search_docs", "send_reply", "create_escalation", "set_category"})

elif t == "tool_use":
    ...
    if full_fidelity and raw_tool in _DEMO_RAW_TOOLS:
        step["input"] = raw_input
elif t == "tool_result":
    ...
    if full_fidelity and payload.get("tool") in _DEMO_RAW_TOOLS:
        step["result"] = payload.get("result")
```

and, because `send_reply.body` / `create_escalation.reason` / the `text` steps are model prose that restates whatever `lookup_customer` returned, either (a) scrub the ticket's own address before it is stored, or (b) reject a `/tickets` POST from the `demo` tier whose `customer_email` is not one of `SEED_CUSTOMERS` — which is what the Try-it form already does client-side (`dashboard.html:736-741`) for exactly this threat (T-06-27). A client-side control for a server-side threat on a route whose key is published is not a control.

---

### CR-02: the test that certifies CR-01's absence pins the disclosure instead, and its "withheld by value" assertion is a fixture artifact

**Severity:** CRITICAL / BLOCKER — a shipped test asserting a false safety property; it is the mechanism by which CR-01 shipped and by which it will re-ship
**File:** `tests/test_dashboard.py:1755-1813` (`test_a_demo_originated_run_is_full_fidelity`)

**Issue.** The test's docstring makes the claim the phase relies on:

> *"`customer_email` is still withheld (Q3) … The ticket's own address below is a separate sentinel that no tool ever sees, so its absence is a claim about the ENVELOPE and not an accident of the run's script."*

and closes with

```python
1812:    assert "customer_email" not in json.dumps(detail)
1813:    assert ticket_address not in json.dumps(detail)
```

Both assertions are true and neither means what the docstring says:

1. **Line 1812 asserts a key name, not a value.** The address is published under the key `"email"` (`lookup_customer`'s declared argument name and its result field), so a grep for the string `customer_email` cannot see it.
2. **Line 1813 is vacuous by fixture construction.** `_demo_ticket(client, ticket_address, …)` at :1774 gives the demo ticket a *different* address (`demo-ticket-address-1c9e55@example.com`) from the one `_script_the_four_vector_run` makes the model look up (`DRILL_EMAIL`). In production the two are the same address — the Try-it form sends `customer_email: mia@datalane.ai` and the model then calls `lookup_customer(email="mia@datalane.ai")`. The test's own separation of the two is the only reason :1813 passes.
3. **Line 1794 actively pins the disclosure:** `assert lookup_result["result"]["customer"]["email"] == DRILL_EMAIL`. The suite does not merely fail to catch CR-01; it asserts that CR-01's payload is present and would go red if someone fixed it.

**Named mutation (executed).** Change :1774-1775 to the production shape — `_demo_ticket(client, DRILL_EMAIL, …)`, the ticket's own customer being the one the agent looks up — and add the assertion the docstring claims to be making:

```python
assert DRILL_EMAIL not in json.dumps(detail)
```

Result:

```
AssertionError: a third party's address is on the public route
  'drill-sentinel-4e81c7@example.com' is contained here:
    "email": "drill-sentinel-4e81c7@example.com"}}, {"seq": 3, "type": "tool_result", …
    "result": {"found": true, "customer": {"email": "drill-sentinel-4e81c7@example.com",
    "name": "Drill Sentinel", "plan": "enterprise", …},
    "recent_tickets": [{"id": 1, "subject": "API limits", …}]}}
```

With :1774 left as shipped, `assert ticket_address not in json.dumps(detail)` remains green — `ticket_address` is by then a string that appears nowhere in the run at all.

**Fix.** After CR-01 is fixed, make the demo test use one address for both the ticket and the lookup (the production shape), and assert the address's **value** is absent, not its column name. Keep a second sentinel for the `recent_tickets` subjects — a distinct improbable string seeded as an *earlier* ticket's subject for the same address — because that is an independent vector no current test covers at all.

---

## Warnings

### WR-01: the model-chosen tool name is clamped on exactly one of the four branches that publish it

**File:** `src/relay/events.py:206-266` (`_project_tool_result`), `:476-478` (the clamp), `:516-542` (guardrail / notice)
**Issue.** `project_run_detail`'s docstring (`events.py:359-364`) states the control as general: *"Both the tool name and its argument keys are strings the MODEL chose (INFO-1), and both reach a browser: the name is clamped to this map and an unregistered one renders the literal `unknown`."* Only the `tool_use` branch clamps. `_project_tool_result` forwards `d.get("tool")` verbatim (`:222, :231, :266`), and the `guardrail` (`:522-524`) and `notice` (`:538-539`) branches forward `guard`/`tool`/`action`/`kind`/`cause` verbatim. `agent.py:522-527` builds the `tool_result` event from `block.name` — the raw model-chosen string — and `agent.py:468` does the same for the guardrail event.

Executed probe, drill-down public branch and live `project()`:

```
tool_use     -> {'tool': 'unknown'}                                    # clamped
tool_result  -> {'tool': 'XXXX…<img src=x onerror=alert(1)>', 'denied_by': 'XXXX…'}
guardrail    -> {'guard': 'XXXX…', 'tool': 'XXXX…', 'action': 'XXXX…'}
notice       -> {'kind': 'XXXX…', 'tool': 'XXXX…', 'retrieval_mode': 'XXXX…', 'cause': 'XXXX…'}
```

This is **not** XSS — the page writes every one of these through `textContent` — but it defeats the stated control, leaves an unbounded model-controlled string on a public endpoint (Phase 5's INFO-1, which this clamp was written to close), and the one place a length bound exists is the branch that needed it least.

**Fix.** Clamp in `_project_tool_result` where the name is actually read, so every branch inherits it:
```python
def _project_tool_result(d: dict, *, known: frozenset[str] = frozenset()) -> dict:
    tool = d.get("tool")
    tool = tool if tool in known else "unknown"
```
and pass `known_tools.keys()` from both `project_run_detail` and the `/events` publish site; apply the same clamp to `guardrail.tool` and coerce `guard`/`action`/`kind`/`cause` against their enumerated sets (the WR-02 lesson `events.py:306-313` already writes down for `notice.results`, applied to the other unconstrained forwards).

---

### WR-02: the drill-down's private rate-limit bucket is unreachable, and a drill-down flood still breaks the visitor's live feed — the property the bucket was created for

**File:** `src/relay/main.py:148-153` (`_gate._dependency`), `:186-192` · `src/relay/config.py:46, 68, 159` · `src/relay/ratelimit.py:71-76`
**Issue.** `run_detail` gets its own bucket with a stated rationale repeated in three places (`main.py:188-191`, `ratelimit.py:71-76`, `config.py:156-158`): *"a visitor clicking through the back catalogue would otherwise spend the live feed's reconnect allowance and silently break their own feed."* But `_gate` charges the **shared** anon bucket first, on every public route:

```python
async def _dependency(request, presented=_API_KEY):
    await enforce("auth", "anon", request)     # 60/minute, SHARED with /events
    if public:
        await enforce(bucket, "anon", request) # 120/minute for run_detail
```

`anon_auth_limit` is `60/minute` (`config.py:46`); `anon_run_detail_limit` is `120/minute` (`config.py:159`). The drill-down's own bucket is **twice the size of the bucket that gates it**, so it can never be the binding constraint — and every drill-down open still spends one unit of the same window `/events` connects spend.

**Executed proof** (`anon_auth_limit="2/minute"`, both route buckets at `1000/minute`):

```
run_detail codes: [404, 404, 429] then /events -> 429
log: {"event":"ratelimit.exceeded","bucket":"auth","tier":"anon", …}   ← auth, not run_detail
```

The refusal comes from `auth`, and the subsequent `/events` connect is refused too. With shipped defaults, 60 drill-down opens in a minute leave the visitor unable to reconnect their feed — and because `EventSource` treats a non-200 response as a terminal failure (it does not retry a 429), the feed goes to `readyState CLOSED` and the page renders "feed closed — reload to watch again". `test_run_detail_is_rate_limited_per_ip` monkeypatches `anon_run_detail_limit` to `1/minute`, so it never observes which bucket actually binds.

**Fix.** Either raise `anon_auth_limit` above every public route bucket it precedes, or exempt public routes from the shared credential-guessing bucket (it exists to meter *key guessing*, and a `public=True` gate resolves no credential):
```python
async def _dependency(request, presented=_API_KEY):
    if public:
        await enforce(bucket, "anon", request)   # its own bucket, and only its own
        return None
    await enforce("auth", "anon", request)
    ...
```

---

### WR-03: `/metrics` is still the only route with no perimeter at all, and this phase made its per-call cost heavier while the comment above the queries claims the opposite

**File:** `src/relay/main.py:483-500` · `src/relay/telemetry.py:137-141, 143-169, 289-316` · `.planning/phases/06-dashboard-experience/deferred-items.md`
**Issue.** `@app.get("/metrics")` carries no `dependencies=` and takes no `Request` — it is not metered even by the anon bucket, and the page polls it every 5s per open tab (`dashboard.html:289`). Meanwhile the module comment at `telemetry.py:139-141` asserts:

> *"Every one of them replaces a Python aggregation over a full materialisation of `runs` — a read whose cost grew for the life of the volume."*

and `deferred-items.md` records *"every read is aggregated or bounded (`LIMIT 20`, the daily `WHERE`)"*. Measured with `EXPLAIN QUERY PLAN` over a 500-row `runs`:

| query | plan |
|---|---|
| `TOTALS_SQL` | `SCAN runs` |
| `OUTCOMES_SQL` | `SCAN runs` + temp b-tree group-by |
| `OUTCOME_DISTRIBUTION_SQL` | `SCAN runs` + two temp b-trees |
| `GLOBAL_PERCENTILE_SQL` **(run twice, p50 and p95)** | `SCAN runs` + **`USE TEMP B-TREE FOR ORDER BY`** |
| `LAST_RUNS_SQL` | bounded (reverse rowid, `LIMIT 20`) ✅ |
| `DAILY_BUCKETS_SQL` | `SEARCH runs USING INDEX idx_runs_created_at` ✅ |

Four of six still scan the whole table and the percentile pair **sorts** it — twice per request — where the previous Python code sorted once. All six run inside one `asyncio.to_thread` (`main.py:500`) that holds `Database`'s process-wide `RLock` for their whole duration, and that is the same lock `RunRecorder.record` needs once per agent step (`events.py:696-697`). `main.py:157-162` documents a measured **0.81s** loop stall from lock contention on this connection and a 3s container `HEALTHCHECK`.

Concrete: with the Fly volume holding ~50k `runs` rows (a year of a demo anyone can drive at 5 runs/hour/IP), a single unauthenticated `while :; do curl -s $H/metrics; done` holds the DB lock continuously — starving every in-flight paid run's per-event write, which `main.py:381-419` turns into `error:persistence_failed` and *ends the run*. This is the same argument Phase 5's CR-01 made about `/events`, on a route that never got the fix.

**Fix.** Give `/metrics` `dependencies=[Depends(_gate("metrics", public=True))]` with its own generous bucket, and bound the four unbounded queries — the cards do not need a lifetime aggregate that no one can read at a glance. Correct the `telemetry.py:139-141` comment and the deferred-items note to describe what the queries actually do; a comment asserting a bound the code does not have is worse than no comment, because the next reader will not re-measure.

---

### WR-04: `budget_snapshot` on the ungated `/metrics` iterates a module-level dict from a worker thread that the event loop concurrently writes

**File:** `src/relay/ratelimit.py:178-187` (`reserved_usd`), `:161-169` (`spent_today`), `:210-235` (`budget_snapshot`) · `src/relay/main.py:494, 500` · `src/relay/main.py:296` (`reserve_run`)
**Issue.** `reserved_usd` ends in

```python
return sum(usd for _, usd in _reservations.values())
```

That generator is interpreted Python iterating a live dict view, so it yields the GIL between items. `_reservations` is written from the **event loop** by `reserve_run()` (`main.py:296`) and `release_run()` (`main.py:462`), while `budget_snapshot` now runs from a **worker thread** on `/metrics` (`main.py:494` inside `asyncio.to_thread(_read)`). A `reserve_run()` that resizes the dict mid-iteration raises `RuntimeError: dictionary changed size during iteration`.

The race pre-existed via `_gate(meter_spend=True)`, but this phase widened it by orders of magnitude: `/metrics` is **ungated** and polled **every 5 seconds by every open tab**, so the window is now open continuously rather than once per `/process`.

Concrete: a visitor clicks "send it" (`reserve_run` on the loop) while another tab's 5s poll is inside `reserved_usd` → `/metrics` returns 500 → `refresh()` (`dashboard.html:280-288`) does `renderCards(m)` on `{"detail":"Internal Server Error"}` → `TypeError: m.latency_ms is undefined` → the unhandled rejection is silent and the cards, bars, both charts and the runs table all keep showing stale data until the next tick.

**Fix.** Snapshot under a lock, or copy before summing:
```python
def reserved_usd(now: float | None = None) -> float:
    now = time.monotonic() if now is None else now
    with _reservations_lock:          # threading.Lock, held for the prune + the sum
        _prune(now)
        return sum(usd for _, usd in list(_reservations.values()))
```
and give `refresh()` a `try/catch` that leaves the last good render in place rather than half-updating it.

---

### WR-05: the demo-key substitution is context-blind — one HTML escaper feeds both an HTML text node and a JavaScript string literal

**File:** `src/relay/main.py:747-760` (`dashboard()`) · `src/relay/templates/dashboard.html:98` (HTML context) and `:725` (JS context)
**Issue.** `DASHBOARD_HTML.replace("__RELAY_DEMO_KEY__", escape(settings.demo_key))` substitutes the *same* escaped string into two different parsing contexts. `html.escape()` is correct for `:98` (`<code>X-API-Key: __RELAY_DEMO_KEY__</code>`) and wrong for `:725` (`const DEMO_KEY = "__RELAY_DEMO_KEY__";`), because `<script>` is raw text — entity references are **not** decoded inside it.

Two concrete failures, both reachable from a single `fly secrets set`:

1. **Any key containing `& " ' < >`.** `escape("k&y")` → `k&amp;y`. The `<code>` block renders `k&y` correctly (the browser decodes it), while `DEMO_KEY` holds the literal seven characters `k&amp;y`. Every Try-it submission then sends a wrong `X-API-Key` and gets 401 — rendered by `renderRefusal` as "this run was refused" — while the page beside it displays the correct key. Undiagnosable from either the page or the logs.
2. **A key ending in `\`.** `escape()` does not escape backslash. `const DEMO_KEY = "abc\";` escapes the closing quote; the string literal runs into the next line and the script fails to parse. The **entire page** dies — cards, distribution, both charts, the gauge, the live feed, the drill-down and Try-it — because it is one `<script>` block. The server returns 200 with a perfectly valid-looking body, so nothing on the server side notices.

`test_dashboard_substitutes_the_key_per_request` only ever uses `test-demo-key` and `rotated-key-91c4de`.

**Fix.** Escape per context, and validate the key at config load:
```python
published = settings.demo_key or ""
html_key = escape(published) if published else "(not configured)"
js_key = json.dumps(published or "(not configured)")   # a complete JS string literal
return (DASHBOARD_HTML
        .replace('"__RELAY_DEMO_KEY_JS__"', js_key)
        .replace("__RELAY_DEMO_KEY__", html_key))
```
plus a `field_validator` on `demo_key` rejecting anything outside `[A-Za-z0-9._-]`, and a test that drives a key containing `&"'<>\`.

---

### WR-06: an error anywhere in the Try-it stream loop permanently disables the form, with the status line stuck on "working…"

**File:** `src/relay/templates/dashboard.html:820-847` (`submitTryIt`), `:853-894` (`streamRun`)
**Issue.** `trySend.disabled = true` is set at `:821` and cleared at exactly one place, `:846`, which is reached only if `streamRun` returns normally. `streamRun` guards the *initiating* `fetch` in a `try/catch` (`:855-863`) but the streaming read is unguarded:

```js
870:  const reader = res.body.getReader();
874:    const { value, done } = await reader.read();
890:      if (data) onTryFrame(name, JSON.parse(data), uid);
```

`reader.read()` rejects on any mid-stream transport failure — a dropped Wi-Fi connection, a Fly proxy reset, the machine autostopping, a deploy landing during a 20-second run. That rejection propagates out of `streamRun` → out of `submitTryIt` → an unhandled promise rejection in the click handler. `:846` never runs, so **"send it" stays disabled for the life of the page**, and `tryState` stays on `"ticket #N — working…"` forever. The visitor's only recourse is a reload — on the page that is the phase's call to action, in the failure mode a scale-to-zero demo produces most often. `res.body` can also be `null`, and `JSON.parse(data)` is unguarded on the same path.

**Fix.** Wrap the read loop and use `finally` for the button state:
```js
async function streamRun(ticketId) {
  ...
  try {
    if (!res.body) { tryFailed("the run started but the stream is unavailable."); return; }
    const reader = res.body.getReader();
    ... /* loop, with JSON.parse in its own try */
  } catch (err) {
    tryFailed("the connection dropped mid-run — the run may still have finished."
              + " Open the trace to see.");
  }
}
// and in submitTryIt:  try { await streamRun(ticket.id); } finally { trySend.disabled = false; }
```

---

### WR-07: `openDrill` has no request-generation guard — the dialog can render a different run than the one clicked

**File:** `src/relay/templates/dashboard.html:499-522`
**Issue.** `openDrill` opens the dialog synchronously, then `await`s the fetch and renders whatever comes back. Two opens in flight resolve in **arrival** order, not click order.

Concrete: a visitor clicks run A in the "Recent runs" table (slow response — `/runs/{uid}` reads `run_events`, `runs` and `tickets`), then immediately clicks run B in the live feed. B resolves first and renders; A resolves second and overwrites the panel. The dialog is now titled `Ticket #<A> · run <A[:8]>` with A's steps, while nothing the visitor did asked for A a second time. On a run that happens to be demo-origin, the panel shows A's raw ticket text under a badge reading "You submitted this run" — for a run the visitor may not have submitted.

**Fix.** A monotonic token, checked before rendering:
```js
let drillGeneration = 0;
async function openDrill(uid) {
  const mine = ++drillGeneration;
  ...
  const resp = await fetch(...);
  if (mine !== drillGeneration) return;      // a newer open superseded this one
  ...
}
```

---

### WR-08: the page's entire test surface — five grep tests plus the CI docker smoke — survives the deletion of `el()`'s only rendering statement

**File:** `tests/test_dashboard.py:1914-1935, 2219-2244, 2154+, 2246+, 2289+` · `.github/workflows/ci.yml:42-65`
**Issue.** Every front-end assertion is a token search over the served document. The suite therefore proves the *absence* of forbidden tokens and the *presence* of permitted ones, and nothing about whether the page renders. The wave header (`tests/test_dashboard.py:1828-1835`) says so honestly — but the specific mutation below is a plausible refactor rather than an adversarial alias, and it takes the whole page out while every test stays green.

**Named mutation (executed, then reverted).** In `dashboard.html:175`:
```diff
- if (text !== undefined) n.textContent = text;   // textContent, always
+ if (text !== undefined) n.setAttribute("title", text);
```
Every value the page renders through `el()` — every card, every distribution row, every chart label, every drill-down fact, every step line, every chip, every refusal — disappears. Result:

```
407 passed in 3.93s
```

`test_dashboard_never_renders_through_a_markup_sink` still passes because the token `textContent` survives elsewhere (`svg()` at `:304`, `line.textContent` at `:1065`, `feedStatus.textContent`, `drillTitle.textContent`), `createElement` survives, and no sink appeared.

**CI docker smoke, same class.** `.github/workflows/ci.yml:55` asserts `curl -sf .../dashboard | grep -q "Relay"` — satisfied by `<title>Relay dashboard</title>` alone. Replace the template with a bare `<html><head><title>Relay dashboard</title></head><body></body></html>` and the job prints `smoke ok`. The two failure modes the comment at `:30-41` names are only partly covered: a *missing* template is caught earlier (the import at `main.py:744` raises and `/health` never comes up), and what the curl uniquely adds — a *served but broken* page — is exactly what the grep cannot see. Separately, `docker run` at `:44` mounts **no volume**, so `_add_column_if_missing` — whose entire stated purpose is being safe against a pre-existing table on the live Fly volume (`db.py:269-282`) — is never exercised against an existing DB anywhere in CI.

**Fix.** Two cheap steps, neither requiring a full browser:
1. Add a minimal DOM test. `pip install lxml` is already transitively present via nothing, but `html.parser` + a tiny JS-free assertion is not the answer — the honest cheap option is a single Playwright/jsdom smoke in its own optional CI job that loads `/dashboard` against a seeded DB and asserts `#cards` has seven non-empty `<b>` elements and `#drill-steps` renders after `openDrill`. One test closes the whole class.
2. Make the docker smoke assert content only the shipped page has (`grep -q 'id="try-examples"'` **and** `grep -q "openDrill"`), and run the container a **second time against the same `-v` volume** so the migration path is what CI actually exercises.

---

### WR-09: the p50 card and the p50 chart line are computed over different populations, on a page whose whole purpose is credibility

**File:** `src/relay/telemetry.py:160-169` (global, unwindowed), `:213-236` (per-day, windowed), `:310-311, 314`
**Issue.** The comment at `telemetry.py:213-216` says *"The rank expression is character-for-character the one in `GLOBAL_PERCENTILE_SQL`, so the chart's p50 and the card's p50 are the same statistic."* The rank **expression** is identical; the **population** is not. `GLOBAL_PERCENTILE_SQL` has no `WHERE` — it is every run for the life of the volume. `DAILY_BUCKETS_SQL` is bounded to `metrics_window_days` (14) and partitioned per day.

Concrete: 100 runs at 5000ms twenty days ago plus 3 runs at 200ms today gives a "p50 ms" card reading **5000** directly above a latency chart whose only plotted point sits at **200**. A visitor reading both concludes the page is broken — which is the exact failure the shared rank expression was introduced to prevent, displaced from the rounding to the window.

**Fix.** Either window the card the same way (`WHERE created_at >= datetime('now', ?, 'start of day')` in `GLOBAL_PERCENTILE_SQL`, which also fixes half of WR-03), or label the card "p50 (all time)" and the chart "p50 per day (last 14 days)". Correct the comment either way.

---

### WR-10: the public feed's step describers render `undefined`/`null` straight into the DOM

**File:** `src/relay/templates/dashboard.html:1019-1029` (`describe`), `:907-924` (`describeOwn`)
**Issue.** The drill-down has `dash()` (`:490`) for exactly this and uses it consistently. Neither feed describer does:

```js
1024:  if (f.type === "usage")      return "step " + f.steps + " · $" + f.cost_usd;
1026:  if (f.type === "error")      return "error · " + f.reason;
1027:  if (f.type === "notice")     return "notice · " + f.kind + " · " + f.tool;
 914:  if (name === "usage")        return "step " + d.steps + " · $" + d.cost_usd;
```

Every one of those fields is `d.get(...)` in `project()` (`events.py:283-317`) and is `null` whenever the source event omits it. An `error` frame with no `reason` — `project()` publishes `reason: None` for any error event whose data lacks the key — renders the literal line `error · null` on the public live feed. `renderChunks` (`:690`) has the same shape: `r.doc + " · " + r.id` gives `null · null` for a malformed result row, and `project_run_detail` explicitly tolerates malformed rows rather than dropping them at that granularity.

**Fix.** Route every interpolated frame value through `dash()`, which already exists two hundred lines above and is the page's own stated convention.

---

## Info / Notes

### NT-01: `budget_snapshot` publishes a rounded spend but gates on the unrounded one
`src/relay/ratelimit.py:229-234`. `spent_today_usd` is `round(spent, 4)` while `exhausted` is `spent >= ceiling` on the raw float. At `spent = 4.99997` and `ceiling = 5.0` the gauge renders `$5.0 / $5.0` and `$0.0 left today` in the *non*-exhausted colour (`dashboard.html:431`), with `/process` still admitting runs. D-11's "structurally incapable of disagreeing" holds for the decision; the rendered numbers can still read as a contradiction. Compare on the same rounded value.

### NT-02: the `tool_use`/`tool_result` pairing is LIFO and is correct only because `agent.py` interleaves emission
`src/relay/events.py:405-413`. `stack.pop()` takes the most recent pending `tool_use`. That is right for `agent.py`'s current per-block loop (`use(A) → result(A) → use(B) → result(B)`, `agent.py:341-540`). The natural future refactor — emit every `tool_use` of a response, then execute them, then emit every `tool_result` — makes LIFO **wrong**: two `search_docs` in one turn pair `result#1↔use#2` and `result#2↔use#1`, producing negative `duration_ms` and, for two `send_reply` attempts, a `cited` set taken from the wrong call. That last one is the "view contradicts its control" failure `retrieval.normalise_citation`'s docstring (`retrieval.py:143-147`) calls worse than no view. Either use `pop(0)` and a comment naming both orderings, or assert the interleaving in a test that scripts two calls to the same tool in one response.

### NT-03: the retention sweep is a full table scan on every cold start, awaited before the app serves
`src/relay/main.py:63-65` · `src/relay/db.py:258-265, 83-87`. `DELETE FROM run_events WHERE created_at < …` has no supporting index — the only index on that table is `idx_run_events_run_uid` (added this phase). On `min_machines_running=0`, every visitor triggers a cold start and therefore a full scan of a table that grows ~10 rows per run for the life of the volume, held under `Database`'s lock, before `/health` can answer. Add `CREATE INDEX IF NOT EXISTS idx_run_events_created_at ON run_events(created_at)`.

### NT-04: `el()`'s `setAttribute` loop is an unguarded attribute sink and no test covers it
`src/relay/templates/dashboard.html:169-177`. Every attribute value today is a literal from this file, so there is no live issue — but `n.setAttribute(k, attrs[k])` will happily set `href`/`src`/`formaction` from server data the day someone passes one, and `_MARKUP_SINKS` (`tests/test_dashboard.py:1883`) greps only for markup sinks. A `javascript:` URL needs no `innerHTML`. Add `href`/`src`/`srcdoc`/`formaction`/`on` to the forbidden-token list, or refuse those keys inside `el()`.

### NT-05: `/runs/{uid}` truncation is silent
`src/relay/main.py:542-546`, `settings.run_detail_max_events = 400`. A run whose events exceed the limit renders its first 400 steps with nothing in the response saying so, and `status` still reads `"complete"`. `max_agent_steps` makes this unreachable today; a `"truncated": true` flag costs one line and one `LIMIT ? + 1`.

### NT-06: money is formatted two different ways on the same page
`dashboard.html:204-205` (`"$" + m.cost_usd.total` → `$0.1`), `:273` (`"$" + r.cost_usd.toFixed(4)` → `$0.1000`), `:436-437` (`"$" + spent` → `$0.0213`). Cosmetic, on the page whose stated bar is "cost legible in under a minute".

### NT-07: `test_the_page_never_asks_for_full_fidelity` is self-declared as passing on write
`tests/test_dashboard.py:2414-2431`. The docstring says so plainly, which is the right handling. Noted only so the vacuity audit is complete: its `assert token not in html` form is also brittle — a legitimate future `full=` anywhere in CSS or copy reds it for no security reason.

### NT-08: `.gitignore` now ignores every PDF in the repository
`.gitignore` (quick task `d21cfc6`). `*.pdf` is repo-wide to catch checkpoint browser exports. The repo ships no PDF assets today, so this costs nothing today; the day someone adds a design artifact or a spec PDF it will be silently untracked. `*.pdf` scoped to the repo root, or an explicit `Relay dashboard.pdf`, would have the same effect with none of the surprise.

### NT-09: `outcomes` on `/metrics` has an unbounded key set and nothing reads it
`src/relay/telemetry.py:152-154, 293, 299`. The comment concedes *"nothing on the page reads it"*. It is a public API shape that grows a key per distinct `runs.outcome` string. Bounded today by the single `record_run` call site; a candidate for deletion rather than for a second consumer.

### NT-10: `guardrail` and `notice` forward their fields without the coercion `project()`'s own WR-02 lesson prescribes
`src/relay/events.py:299-317, 516-542`. `guard`, `action`, `kind`, `cause`, `retrieval_mode` are forwarded raw where `result_count` and the two ticket ids are coerced. All are agent literals today; the asymmetry is the same shape as the WR-02 finding this phase was praised for closing. Covered operationally by WR-01's fix.

### NT-11: `refresh()` has no failure path
`dashboard.html:280-289`. Any `/metrics` failure (WR-04's 500, a cold-start timeout) throws inside `renderCards` and leaves the page half-updated with a silent unhandled rejection. `setInterval` self-heals on the next tick, so the impact is a 5-second stale window — but on first load it is an empty page with no explanation.

---

## Test-Quality Audit

| Test | Verdict | Evidence / named mutation |
|---|---|---|
| `test_a_demo_originated_run_is_full_fidelity` (:1755) | **Certifies a false property.** | CR-02. Line 1794 pins the disclosure; line 1813 is vacuous because the fixture gives the ticket a different address from the one the model looks up. Mutation executed. |
| `test_run_detail_never_leaks_a_non_demo_runs_content` (:1635) | **Sound and load-bearing.** | Presence proved twice (owner SSE body + raw `run_events` rows) before any absence; four sentinels on four distinct payload fields; per-step × per-sentinel collection; anti-vacuity assertions on step types, tool names and the *redacted shape* (`arg_keys == ["email"]` and `"input" not in lookup`). I could not construct a partial-redaction mutation that passes it. |
| `test_full_fidelity_is_server_decided` (:1712) | **Sound.** | Byte-compares tampered vs. plain content, so a reordered-but-widened response cannot pass. Covers query params, four headers and a cookie. |
| `test_a_legacy_null_origin_run_is_redacted_at_the_route` (:1493) | **Sound.** | Inserts via raw SQL (the pre-migration shape), asserts the sentinel is in the raw rows first, then asserts absence *and* `detail["steps"]` non-empty. The `origin != "owner"` fail-open mutation reds it. |
| `test_the_window_bounds_the_chart_not_the_ledger` (:373, test_metrics) | **Sound, and unusually well built.** | Asserts `DAILY_BUCKETS_SQL`'s own rows **and** its `EXPLAIN QUERY PLAN`, precisely because densifying onto the window's day list would hide an unbounded read from a `run_metrics`-only assertion. This is the shape the brief was worried about, done right. |
| `test_percentile_is_half_up` (:99, test_metrics) | **Sound.** | Drives the real `GLOBAL_PERCENTILE_SQL` against a real SQLite for 60×3 pairs and asserts zero disagreements, plus an anti-vacuity tie assertion. I independently confirmed SQLite `ROUND` is half-away-from-zero. |
| `test_daily_percentiles_match_the_oracle` (:331, test_metrics) | **Sound.** | Fixed-seed randomisation with explicit assertions that the n=0 and n=1 cases were actually drawn. |
| `test_dashboard_never_renders_through_a_markup_sink` (:1914) and the four sibling grep tests | **Green under a page-killing mutation.** | WR-08. Replacing `n.textContent = text` with `n.setAttribute("title", text)` in `el()` leaves **407 passing** with every rendered value gone. Executed. |
| CI `docker` job smoke (`ci.yml:42-65`) | **Near-vacuous for its stated purpose, and misses the migration path entirely.** | `grep -q "Relay"` is satisfied by `<title>` alone; no volume is mounted, so `_add_column_if_missing`'s "safe on the live volume" claim is never exercised against an existing DB in CI. |
| `test_run_detail_is_rate_limited_per_ip` (:1404) | **Behaviourally sound; its docstring asserts a property the code does not have.** | The 429 is real, but it monkeypatches `anon_run_detail_limit="1/minute"` so it never observes that `auth`/anon (60/min) binds first — WR-02, proven live. |
| `test_the_page_never_asks_for_full_fidelity` (:2414) | **Honestly labelled.** | Self-declares "passed the moment it was written". NT-07. |
| `test_phase6_migrations_are_idempotent` (:50) | **Sound.** | File-backed `tmp_path` rather than `:memory:`, precisely so the "table already exists" path is real. The named mutation (drop the PRAGMA guard) does raise. |

**Coverage gaps not attributable to any one test:** no test drives a demo run whose ticket address is the one the agent looks up (CR-01/CR-02); no test seeds a *prior* ticket for the looked-up address, so the `recent_tickets` disclosure vector is untested in either direction; no test exercises which rate-limit bucket actually binds on a public route (WR-02); no test drives `/metrics` concurrently with a `reserve_run()` (WR-04); no test renders anything in a DOM (WR-08); and no test drives `dashboard()` with a demo key containing a shell/JS metacharacter (WR-05).

---

_Reviewed: 2026-08-13_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
_Working tree restored; `pytest -q` == 407 passed and `ruff check src tests` clean at finish._
