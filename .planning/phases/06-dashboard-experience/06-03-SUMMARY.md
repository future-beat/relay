---
phase: 06-dashboard-experience
plan: 03
subsystem: api
tags: [redaction, allowlist, sqlite, sse, citations, security-boundary]

requires:
  - phase: 05-run-event-persistence-live-feed
    provides: "run_events rows, project()/_project_tool_result, RunRecorder, attribute_to_run's W-1 condition"
  - phase: 06-dashboard-experience
    provides: "06-01's elapsed_ms stamping and tickets.origin (the demo flag full_fidelity is derived from)"
provides:
  - "events.project_run_detail(rows, *, full_fidelity, known_tools) — the drill-down redactor, this phase's security boundary"
  - "retrieval.normalise_citation — the one normalisation the citation guard and the drill-down share"
  - "a corrected events.py module docstring (three public serialisers) and attribute_to_run docstring (the resolved W-1 position)"
affects: [06-04 run-detail route, 06-05/06-06 dashboard rendering]

tech-stack:
  added: []
  patterns:
    - "Second allowlist, not a spread: the demo/full-fidelity exception names every field it adds"
    - "Delegate the risky branch to the stricter existing redactor so the wider surface cannot out-disclose the narrower one"
    - "Share one normalisation between a control and the view that audits it"

key-files:
  created: []
  modified:
    - src/relay/events.py
    - src/relay/retrieval.py
    - src/relay/agent.py
    - tests/test_dashboard.py

key-decisions:
  - "The public tool_result branch calls _project_tool_result itself rather than restating it, making 'the drill-down can never disclose more of a tool result than /events' a structural property"
  - "full_fidelity is keyword-only with NO default (T-06-11), pinned by a signature assertion because no behavioural test can observe a default every caller overrides"
  - "customer_email is on neither branch (research Q3); retrieved_ids is on neither either — it is not in the allowlist table, and a field nobody wrote down is not published even when it looks harmless"
  - "A malformed payload is a dropped step and a warning log naming seq/type only, never a payload value and never a 500"
  - "cited means 'in a reply the guard ACCEPTED', so a denied attempt's citations are excluded — the guardrail row already tells that story"
  - "run_uid's withholding from /metrics was dropped rather than widened: this plan satisfied the W-1 condition by making the drill-down server-redacted"

patterns-established:
  - "Pattern 1: reuse-as-control — the public branch of a wider surface calls the narrower surface's own redactor"
  - "Pattern 2: named demo allowlist — the D-02 exception is a second list of field names, never {**payload}"
  - "Pattern 3: audit-agrees-with-control — one shared normalise_citation, and a test that drives a real run through the guard and then projects it"

requirements-completed: [DASH-03]

duration: 45min
completed: 2026-08-12
---

# Phase 6 Plan 03: The Drill-Down Redactor Summary

**`project_run_detail` turns one run's stored `run_events` rows into DASH-03's step list through a field-by-field allowlist whose public `tool_result` branch IS the live feed's own `_project_tool_result`, with a second, explicitly-named allowlist for D-02's demo exception.**

## Performance

- **Duration:** ~45 min
- **Tasks:** 3/3
- **Files modified:** 4
- **Suite:** 367 passed (floor was 345; +7 from this plan, the rest from 06-02 landing concurrently)
- **Lint:** `ruff check src tests` clean

## Accomplishments

### Task 1 — one normalisation, shared (commit `88034e4`)

`retrieval.normalise_citation(value)` lands beside `slug()` and the `{doc}#{heading}` id shape those build. `agent.py`'s citation guard now calls it in both places it had open-coded `strip().lower()`; `grep -c "strip().lower()" src/relay/agent.py` is 0 and `grep -c "normalise_citation"` is 3 (import + two call sites). Nothing else in `agent.py` moved — `grep -c "async with"` is still 0.

The docstring names both call sites and states why they must not drift: an audit view that contradicts the control it audits is worse than no audit view, because a reader cannot tell which of the two is lying.

### Task 2 — `project_run_detail` and its demo branch (commit `7615ba8`)

Signature: `project_run_detail(rows, *, full_fidelity: bool, known_tools: dict[str, frozenset[str]]) -> list[dict]`, directly below `project()`.

Three passes:
1. **Parse.** `json.loads` inside `except (ValueError, TypeError)`; a payload that will not parse *or* parses to a non-dict is DROPPED and logged as `run_detail.malformed_payload` with `extra={"ctx": {"seq", "type"}}` — never a payload value.
2. **Run-level facts.** The cited set (from the send_reply `tool_use` whose paired `tool_result` has `is_error` false), and tool_use→tool_result pairing by tool name to the nearest preceding *unpaired* use.
3. **Build.** Envelope `seq`/`type`/`elapsed_ms` (never `created_at`), then per-type fields exactly as the research allowlist table specifies.

Security properties, as built:

| Property | How it is enforced |
|---|---|
| The drill-down cannot out-disclose the feed | The public `tool_result` branch calls `_project_tool_result(payload)` and merges its already-redacted output. Not a matching list — the same function. |
| No `error`/`message` key on the public branch | Inherited from that reuse, and asserted directly (`not any({"error","message"} & set(step))`). |
| The demo branch is a second allowlist | `input`, `result`, `text`, `missing_citations` — each named at its own `if full_fidelity:`. `grep -n "{\*\*" src/relay/events.py` returns exactly one line, `attribute_to_run`'s. |
| `customer_email` excluded on BOTH branches | Neither branch reads it; ticket fields are the route's (06-04) to add, and Q3 keeps the email out even in D. |
| Model-chosen strings clamped | `tool` clamped to `known_tools` else the literal `"unknown"`; `arg_keys` intersected with the tool's declared `input_schema.properties`; the remainder becomes `unknown_arg_count`, a number. |
| `full_fidelity` unreachable from a request | Keyword-only, no default (T-06-11). |
| Fail-closed | Unknown `type` → dropped, on both branches. Malformed payload → dropped. |
| A swept run renders | `rows == []` → `[]`; an orphan `tool_result` with no paired use carries `duration_ms: None`. |

Both docstrings updated:

- **Module docstring** now names THREE public serialisers and explains that `test_events_output_comes_only_from_two_serialisers` still holds and still says two — it pins the `/events` GENERATOR, which must never grow a third path, and the drill-down is a different route with its own tests. The test's intent is preserved, not defeated.
- **`attribute_to_run`** no longer claims `/metrics` withholds the uid (verified false as of 06-02's `cee5c24`, which put `run_uid` back in `_PUBLIC_RUN_COLUMNS`). It now records the coherent position: the uid is published on both surfaces and stays a *correlation* token because the drill-down it opens is server-redacted by the allowlist above (D-01/D-03) and the one full-fidelity path is gated on `tickets.origin`, decided server-side. Phase 5's W-1 condition was **satisfied**, not widened. The "if that is ever not true, this is the line to delete" warning is kept and sharpened with what "not true" would look like.

### Task 3 — the audit provably agrees with the control (commit `480d920`)

A real scripted run (`voyage_api_key` pinned to `None` → keyword mode, free, deterministic) searches two docs, makes one `send_reply` that cites a genuinely retrieved id but names another ticket (the binding guard refuses it), then one that cites a different id in another case (the citation guard accepts it). The projector must mark the accepted reply's chunk cited and the denied one's not. The expected set is derived from the run's own results, and asserted non-empty *and* not-everything before anything is claimed.

## Mutation Log

Every mutation was applied to source, run, confirmed red, and restored. `diff -q` against a pristine backup confirmed restoration after each batch.

| Mutation | Test | Result |
|---|---|---|
| `normalise_citation` returns `value` unchanged | `test_normalise_citation_is_the_guards_normalisation` | RED (both halves — verified independently, see below) |
| Public branch forwards the raw payload | `..._publishes_only_named_fields` | RED — 11 (step, secret) pairs leaked |
| `full_fidelity` gains a default of `True` | `..._publishes_only_named_fields` | RED (signature assertion) |
| Unknown-type fallthrough returns the payload | `..._drops_unknown_and_malformed` | RED |
| `json.loads` allowed to raise | `..._drops_unknown_and_malformed` | RED |
| `arg_keys = sorted(raw_input)` (unclamped) | `test_tool_use_arg_keys_are_clamped` | RED — injected key name appeared verbatim |
| Tool name echoed instead of clamped | `test_tool_use_arg_keys_are_clamped` | RED |
| Demo branch adds nothing (`if False:`) | `..._demo_branch_adds_only_named_fields` | RED |
| Pairing name-blind, last-seen, no popping | `test_steps_carry_seq_and_elapsed_and_tool_durations` | RED |
| Pairing name-blind with a single global stack | same | RED **only after the test was strengthened** |
| `created_at` published in the envelope | same | RED |
| Raw compare on the CITED side | unit + integration | RED on both |
| Raw compare on the LICENSED side | unit | RED (integration: PASSES — stated below) |
| Count the DENIED attempt's citations | unit + integration | RED on both (integration only after adding a real denial) |
| Empty cited-set treated as exceptional | `..._no_reply_was_accepted` | RED |

### Three mutations initially survived. All three were test defects, and all three were fixed rather than reported as success.

1. **Global-stack pairing survived** the original durations test, because its single LIFO ordering made the wrong algorithm produce the right answer by accident. Fixed by adding a FIFO run (two overlapping tools completing in start order), where a global stack pairs each result with the *other* tool's use. Now RED.
2. **Licensed-side raw compare survived** both tests, because every id real retrieval mints is already lowercase. Fixed by adding a hit to the unit test whose own `id`/`anchors` are not normalised — a legitimate shape, since `run_events` is a back catalogue and an older build could have written it. Now RED on the unit test.
3. **Counting denied attempts survived** the integration test, because the original run had no denied attempt. Fixed by scripting a send_reply that cites a genuinely retrieved id and is denied for an *unrelated* reason (ticket binding) — so counting it would flip a real chunk to cited. Now RED.

### Stated plainly — what is a regression guard rather than proof

- **`full_fidelity` has no default** is asserted on the *signature* via `inspect`, not on output. Every call site in the suite passes the flag explicitly, so no leak assertion can observe a default. It is a regression guard, and its docstring says so.
- **Licensed-side normalisation** is pinned by the unit test only. The integration test cannot exercise it and its docstring records that the mutation was run against it and passed.
- **The Phase-5 invariant greps** (`async with` == 0, one `broker.publish` site, frozen files unchanged) are regression guards run by hand this plan, not tests.

## Deviations from Plan

### Auto-fixed issues

**1. [Rule 1 — Bug] Two test expectations were wrong; the source was right.**
- **Found during:** Task 2, then Task 3.
- **Issue:** (a) `test_tool_use_arg_keys_are_clamped` expected `max_results` in `search_docs`' declared keys — the schema declares only `query`, so the projector correctly excluded it. (b) `test_cited_is_false_when_no_reply_was_accepted` scripted an escalation whose `reason` was 13 chars against `CreateEscalationInput`'s `min_length=20`, so the run ended `ended_without_action` and the test was silently about a broken run.
- **Fix:** (a) switched to `send_reply`, which declares two keys, so "sorted, declared only" does real work; added a comment that the clamp follows the *schema the model was shown*, not the executor signature. (b) lengthened the reason and commented why.
- **Commits:** `7615ba8`, `480d920`.

**2. [Rule 2 — Missing critical coverage] Three added assertions/scenarios not in the plan.**
The plan's named mutations were run and three survived; per the mutation discipline these were fixed. Added: the FIFO durations run, the orphan-`tool_result` case, the legacy-cased hit, the denied-but-really-retrieved send_reply attempt, and the `inspect.signature` assertion for T-06-11. Each is documented in the test docstring that owns it, including which mutation it exists to kill.

No Rule 4 (architectural) situations arose. No packages installed.

## Files Not Touched

`src/relay/telemetry.py`, `tests/test_metrics.py`, `tests/test_run_events.py`, `tests/test_observability.py` — owned by the concurrent plan 06-02. `git diff --quiet HEAD` against all four returned clean throughout; every commit staged files by explicit path.

Frozen per D-03: `src/relay/mcp_server.py`, `src/relay/evals.py`, `.github/workflows/evals.yml` — asserted unchanged before each commit.

## Threat Flags

None. The new surface is exactly the one the plan's threat register anticipated (T-06-08 through T-06-12); no endpoint, auth path, file access or schema change was introduced — `project_run_detail` is a pure function over rows the caller already fetched. The route that exposes it, and the tampering assertion that `?full=1` cannot reach `full_fidelity`, are plan 06-04's.

## Known Stubs

None.

## Handoff to 06-04

The route must:
- Build `known_tools` from `app.state.registry` as `{name: frozenset(spec.schema["input_schema"]["properties"])}`.
- Derive `full_fidelity` from `tickets.origin` by **boolean equality against the demo value** — NULL or anything else means redacted. Never a truthiness check.
- `SELECT seq, type, payload, elapsed_ms` (not `created_at` — the projector never reads it).
- Read `run_events` **before** `runs`, per the research absence matrix, and render `[]` from a swept run as `status: "swept"` rather than as an error.

## Self-Check: PASSED

- `src/relay/events.py` — FOUND (`project_run_detail` present, 3 references)
- `src/relay/retrieval.py` — FOUND (`normalise_citation` present)
- `src/relay/agent.py` — FOUND (3 `normalise_citation` refs, 0 `strip().lower()`, 0 `async with`)
- `tests/test_dashboard.py` — FOUND (14 tests, all passing)
- Commit `88034e4` — FOUND
- Commit `7615ba8` — FOUND
- Commit `480d920` — FOUND
- `pytest -q` → 367 passed (>= 345 floor)
- `ruff check src tests` → All checks passed
