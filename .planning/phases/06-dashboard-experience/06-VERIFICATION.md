---
phase: 06-dashboard-experience
verified: 2026-08-14T08:40:00Z
status: human_needed
score: 12/12 must-haves verified
behavior_unverified: 2
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 9/12
  previous_verified: 2026-08-14T07:05:00Z
  head_verified: 4edea40
  gaps_closed:
    - "CR-01's model-prose vector — the anonymous /metrics -> /runs/{uid} walk no longer returns the looked-up customer's name, plan, signed-up date, or another visitor's ticket subject. Re-run by me end to end, not read off the fix's summary."
    - "DASH-05's Try-it wiring — eight independent binding deletions each turn the suite RED (previously all green). The chain trySend -> submitTryIt -> runTryIt -> streamRun -> offerTheTrace -> openDrill is asserted link by link, and each link binds."
    - "WR-10 on the drill panel — renderStepBody's ten raw `s.` fields now route through dash(), and both holes the first pass proved green (an `s.` field, and a bare el() text argument in a scanned renderer) now red the guard."
  gaps_remaining: []
  regressions: []
  new_findings:
    - id: NF-1
      severity: warning
      statement: >-
        The mask is whole-token, so the SINGLE most likely disclosure form of a
        multi-word name is not covered: harvested literal "Mia Torres", model prose
        "Hi Mia," -> "Mia" survives onto the keyless route. prompts.py drives exactly
        this ("Address the customer by name"). Same class: "Mia\nTorres" (line break)
        and "Mia  Torres" (double space) both survive. Not the conceded PARAPHRASE
        vector — these are sub-tokens and whitespace variants of the literal itself.
    - id: NF-2
      severity: warning
      statement: >-
        `authored` is a visitor-controlled UNMASKING ORACLE. A harvested literal is
        dropped from the mask when it equals the visitor's own subject after
        strip()+casefold(). Confirmed live on all three target classes: submitting a
        ticket whose subject is "pro" republishes the customer's plan; "Mia Torres"
        republishes the name; the other visitor's exact subject republishes that
        subject. "  PRO  " works too (padding and casing are normalised away).
        `withheld_from_run`'s docstring claims this "cannot" happen because "producing
        the match requires already holding the value" — true for possession, false for
        CONFIRMATION: it converts a guess into a verified fact, one guess per submitted
        ticket, and `plan` has three publicly documented values.
    - id: NF-3
      severity: warning
      statement: >-
        Five named properties of the CR-01 fix are UNGUARDED — each deleted, full suite
        run, 420 green: case-insensitive matching (re.IGNORECASE), dict-KEY masking,
        allowlist-membership harvesting (vs. a hardcoded lookup_customer name),
        missing_citations masking, and `_ordered`'s longest-first ordering. All five are
        correct in the shipped tree; none is regression-bound. Longest-first has a
        demonstrated consequence: alphabetical order leaves "[withheld] asks about
        SSO-INTERNAL-4417" where the shipped order yields "[withheld]".
    - id: NF-4
      severity: info
      statement: >-
        The mask over-masks ordinary English. `recent_tickets[].status` is harvested, so
        "open" and "resolved" are replaced in model prose: "Your ticket is still
        [withheld], and the earlier one is [withheld]." Confirmed live. Cosmetic, on the
        demo's payoff surface, and the same legibility argument `_mask_pattern` makes for
        word boundaries applies here one level up.
behavior_unverified_items:
  - truth: "A visitor can open their own submitted run's full-fidelity drill-down from the Try-it panel (the X-Relay-Run-Uid deep link)"
    test: "Set RELAY_DEMO_KEY, open /dashboard, submit an example, wait for the run to finish, click 'see the full trace'."
    expected: "The dialog opens on THAT run, shows demo=true content (raw tool input, the model's prose, the reply body), and the network tab shows GET /runs/{the uid the response header carried}."
    why_human: "No DOM in the suite. The wiring is now mutation-bound link by link (8 deletions, 8 reds), so this is no longer 'satisfied by a dead definition' — but nothing here dispatches a click. 06-07 checkpoint step 5 remains unwitnessed."
  - truth: "The demo-vs-owner fidelity contrast is visible in the browser (SC-2 / D-02)"
    test: "Open one demo-origin run's drill-down and one owner-origin run's drill-down side by side."
    expected: "The demo panel shows the 'raw — your own run' <details> block; the owner panel shows arg keys and shapes only, with no raw section and no empty holes where it would have been."
    why_human: "Checkpoint step 6, never witnessed. The server-side control is proven by mutation; what is unwitnessed is only the presentation."
human_verification:
  - test: "Set RELAY_DEMO_KEY, open /dashboard, submit an example, click 'see the full trace'."
    expected: "The dialog opens on that run at full fidelity; the request is GET /runs/{X-Relay-Run-Uid}."
    why_human: "Checkpoint step 5, never witnessed. No DOM in the suite."
  - test: "Compare a demo-origin and an owner-origin drill-down in the browser."
    expected: "Raw block present on the demo one, absent (and leaving no hole) on the owner one."
    why_human: "Checkpoint step 6, never witnessed."
  - test: "Read one real demo drill-down's prose end to end, on a run against a seeded address."
    expected: "It should read as English. Watch for '[withheld]' standing where 'open' or 'resolved' belonged (NF-4), and for a bare first name surviving in the greeting (NF-1)."
    why_human: "Legibility is a judgement, and NF-1/NF-4 are the two places the mask's cost and its gap both land on the same sentence."
---

# Phase 6: Dashboard Experience — Verification Report

**Phase Goal:** A visitor can understand the system's cost, quality, and behavior in under a minute — and run it themselves.
**Status:** human_needed (was: gaps_found)
**Re-verification:** **Yes** — second pass, after the three fixes. First pass 2026-08-14T07:05:00Z at `ed3afb2`; this pass 2026-08-14T08:40:00Z at `4edea40` (PR #10 merged).

**Baseline confirmed independently, both passes:**

| | first pass (`ed3afb2`) | this pass (`4edea40`) |
|---|---|---|
| `.venv/bin/python -m pytest -q` | 417 passed | **420 passed** |
| `.venv/bin/ruff check src tests` | All checks passed | **All checks passed** |
| `git status --short` at finish | empty | **empty** |

---

## What happened between the two passes

Three commits, all on `main` via PR #10:

| Commit | Addresses | First-pass finding |
|---|---|---|
| `8532832` | CR-01 prose vector | Gap 1 — anonymous third-party disclosure |
| `b66d017` | DASH-05 wiring | Gap 2 — bindings deletable with the suite green |
| `0c89712` | WR-10 | Gap 3 — `renderStepBody` unwrapped and unscanned |

Net test delta: **+3** (417 → 420). Source touched: `events.py`, `main.py`, `tools.py`, `templates/dashboard.html`.

---

## Method (this pass)

Same standard as the first pass, and nothing below rests on the fix's own commit messages or SUMMARY text:

- **I re-ran the anonymous attack myself**, in the predecessor's exact order — seed a third-party row, submit a demo ticket, drive a scripted restatement run, then `GET /metrics` with **no credential**, harvest a `run_uid`, `GET /runs/{uid}`.
- **25 mutations** applied to source, full suite run for each, source restored byte-for-byte, result recorded. 20 red, 5 green. The 5 greens are new findings, not accepted.
- **Boundary probes** of the mask at both the unit level (`mask_withheld` / `withheld_from_run` directly) and end to end through the live route.
- No paid call made: `settings.voyage_api_key` pinned to `None` on every probe that reaches retrieval; every run driven by `tests/helpers.py::FakeClient`.
- All probe code was deleted before finish; `git status --short` is empty and the suite is back at 420.

---

## Verdict on the three first-pass gaps

### Gap 1 — CR-01's model-prose vector: **CLOSED** for the demonstrated attack

**The attack, re-run by me, with no credential:**

```
seed:    customers row (name "Mia Torres", plan "pro", signed_up 2024-08-30)
         + a ticket filed against that address by SOMEONE ELSE
         ("OTHER-VISITOR-SUBJECT-4417")
run:     a demo ticket -> lookup_customer -> the model restates what it read
walk:    GET /metrics (no key)  -> 200, uid enumerable
         GET /runs/{uid} (no key) -> 200, demo=true
```

Published prose, verbatim from the anonymous response:

```
"Hi Mia, thanks for writing in. [withheld] is on the [withheld] plan
 (signed up [withheld]). Mia's recent tickets include '[withheld]'.
 Mia\nTorres also has an [withheld] item. [withheld] is a [withheld] user.
 Their ticket [withheld] is [withheld]."
```

| Probe | First pass | This pass |
|---|---|---|
| looked-up customer's full name | **reachable** | masked |
| ...as `MIA TORRES` (recased) | — | masked |
| plan, as a word, in model-authored fields | **reachable** | masked |
| `signed_up` date | — | masked |
| another visitor's ticket subject | **reachable** | masked |
| ...lowercased | — | masked |
| visitor's own body (`PROBE-BODY-1`) | present | **still present** (D-02's payoff intact) |
| `[withheld]` marker present | n/a | yes — redaction, not deletion |

Mutation-bound, each run individually against the full suite:

| Mutation | Result |
|---|---|
| drop the `withheld_from_run(parsed, authored=authored)` term | **RED** (2 tests) |
| drop `authored=` at the route's call site | **RED** |
| `_MIN_WITHHOLD_LEN = 4` (so "pro" stops being collected) | **RED** |
| remove the word boundaries from `_mask_pattern` | **RED** |
| delete the route's `withheld = (ticket["customer_email"],)` | **RED** |
| mask `text` but not tool `input` | **RED** (2 tests) |
| mask `input` but not `text` | **RED** (4 tests) |

The fix is the right shape: the mask is **derived in the projector from the run's own rows**, keyed off the same `_DEMO_RAW_TOOLS` allowlist that governs raw payloads, so the caller cannot forget it and a tool added later joins both halves of the rule on the day it is added. That is a structural answer to CR-01's class, not a patch for the one literal that was demonstrated.

**It is not perfect, and three residuals are recorded below as NF-1..NF-3.** None of them re-opens the demonstrated attack.

### Gap 2 — DASH-05 wiring: **CLOSED**

Every binding the first pass deleted with the suite green now reds. I deleted eight, independently:

| Mutation | First pass | This pass |
|---|---|---|
| `trySend.addEventListener("click", submitTryIt)` | 417 green | **RED** |
| `chip.addEventListener("click", () => chooseExample(i))` | 417 green | **RED** |
| `if (uid) offerTheTrace(uid);` | 417 green | **RED** |
| `open.addEventListener("click", () => openDrill(uid))` | — | **RED** (2 tests) |
| `await runTryIt();` | — | **RED** |
| `await streamRun(ticket.id);` | — | **RED** |
| `tryActions.append(open);` | — | **RED** |
| `tryExamplesEl.append(chip);` | — | **RED** |

The new `test_try_it_controls_are_bound_to_their_handlers` asserts the **chain**, not the tokens, and it also pins three positions that make the deep link mean what it says: the offer sits after the `if (!res.ok)` refusal return, after the `X-Relay-Run-Uid` read, and before `res.body.getReader()`. Its `_fn_body` helper asserts the opener is present *before* slicing, so a renamed function fails loudly instead of making the body assertions vacuous — that is the exact failure mode the first pass caught, addressed at the helper level.

**What still rests on the unwitnessed human checkpoint (stated plainly, as asked):** the suite has no DOM. Nothing dispatches a click, nothing constructs an element, nothing observes a handler run. What is proven is that the bindings and call sites are **present and positioned in the shipped source, and unremovable without a red**. That a browser fires them — 06-07 checkpoint **step 5** — is unwitnessed, and no artifact in this repo witnesses it. The uncommitted Node DOM stub the first pass flagged is still uncommitted. Step 6 (the demo-vs-owner presentation contrast) is likewise unwitnessed.

### Gap 3 — WR-10 on the drill panel: **CLOSED**

| Mutation | First pass | This pass |
|---|---|---|
| unwrap `dash(s.tool)` in `renderStepBody` | 417 green | **RED**, names the field |
| unwrap `dash(r.score)` to a bare `el()` text arg in `renderChunks` | 417 green | **RED** |
| unwrap `dash(s.cause)` in the notice branch | — | **RED** |
| unwrap the `STEP_LABELS[s.type] \|\| dash(s.type)` fallback arm | — | **RED** (pinned by example) |
| bare `d.note` (the second `\|\|` render site) | — | **RED** |

`_BARE_FRAME_FIELD` gained the `s.` prefix and a third alternation for a field handed to a call as its last argument; the scan gained `renderStepBody` and `renderSteps`.

**The disclosed blind spot is still only the one site, and it is covered.** I grepped every `||` expression on the page: there are exactly three render sites the regex cannot reach — `STEP_LABELS[s.type] || dash(s.type)` and two `d.note || "<literal>"` sites. The first is pinned by example (red above). The other two are self-defended: their fallback arm *is* a human-copy literal, so a missing field renders product copy rather than `undefined` — and both red anyway, through the refusal-state behavioural tests. I also swept all six scanned renderers for remaining bare `[fdrs].\w+` uses: everything left is a type dispatch, a condition, or an object key. **No render site is unguarded.**

---

## New findings this pass (warnings — none is a blocker)

### NF-1 — the whole-token mask misses the name's most likely form

`mask_withheld` is whole-token by design (`(?<!\w)…(?!\w)`), and the harvested literal is the **whole** `customers.name` value. So:

| Prose the model writes | Published |
|---|---|
| `Mia Torres is on the pro plan` | `[withheld] is on the [withheld] plan` |
| `MIA TORRES here` | `[withheld] here` |
| **`Hi Mia, welcome`** | **`Hi Mia, welcome`** |
| **`Mia\nTorres here`** (line wrap) | **`Mia\nTorres here`** |
| **`Mia  Torres here`** (double space) | **`Mia  Torres here`** |
| `Mia Torres's account` | `[withheld]'s account` |
| `(Mia Torres)` | `([withheld])` |
| `pro-tier user` | `[withheld]-tier user` |
| `processing now` | `processing now` (correctly untouched) |

This is **not** the conceded paraphrase vector — a paraphrase shares no substring; these share a sub-token or differ only in whitespace. And the greeting form is not an edge case: `prompts.py` instructs the model to *"Address the customer by name"*, and models overwhelmingly render that as a bare given name. `withheld_from_run`'s LIMITS block names three limits (paraphrase, min length, non-strings) and does not name this one.

**Cheap closure if wanted:** also harvest the whitespace-split components of a multi-word literal above `_MIN_WITHHOLD_LEN`, and normalise runs of whitespace in the pattern (`\s+` between the parts). Both stay inside the "literal mask" contract.

### NF-2 — `authored` is a visitor-controlled unmasking oracle

`withheld_from_run` drops any harvested literal equal, after `strip().casefold()`, to a string the visitor authored — and `authored` is `(ticket["subject"],)`, which the visitor types. Four live probes, each a real submission followed by the anonymous walk:

| Submitted subject | Published prose |
|---|---|
| `pro` | `The customer [withheld] is on the **pro** plan and previously filed '[withheld]'.` |
| `Mia Torres` | `The customer **Mia Torres** is on the [withheld] plan and previously filed '[withheld]'.` |
| `OTHER-VISITOR-SUBJECT-4417` | `The customer [withheld] is on the [withheld] plan and previously filed '**OTHER-VISITOR-SUBJECT-4417**'.` |
| `  PRO  ` | `…is on the **pro** plan…` — padding and casing are normalised away |

The design doc addresses this and gets it half right:

> *"'drop any harvested value that appears anywhere in the text I submitted' would let a visitor unmask a third party's value by pasting it. Whole-match cannot, because producing the match requires already holding the value."*

Whole-match does block **bulk** unmasking, and that reasoning is why the substring form was rejected — correctly. But "requires already holding the value" conflates possession with **confirmation**. The mask is what makes a value unknowable; an oracle that answers "was your guess right?" is a disclosure primitive in its own right. Cost: one guess per submitted ticket, subject ≤200 chars, demo-tier rate limited. `plan` has three publicly documented values, so **≤3 submissions determine any seeded customer's plan with certainty**.

**Actual impact today: near zero.** The reachable values are the four fictional seeded identities' names and plans — which are literal constants in `src/relay/db.py` in a public repo — and ticket subjects the attacker must already know verbatim. **The claim in the docstring is what is wrong**, and a docstring that overstates a control is the failure shape this phase has now hit twice (CR-02, then the first pass's "proven on one of two vectors, documented as proven on both").

**If closure is wanted:** exempt `authored` only from literals harvested from THIS ticket's own row (compare the recent_tickets entry's `id` against the run's ticket id) rather than by string equality across the whole harvest. That is a strictly narrower exemption and it preserves the payoff the exemption exists for.

### NF-3 — five named properties of the fix are unguarded

Each deleted, full suite run, **420 green**:

| Property deleted | Consequence, demonstrated at the unit level |
|---|---|
| `re.IGNORECASE` in `_mask_pattern` | `"…on the PRO plan (Pro tier)."` publishes unmasked. The docstring calls case-insensitivity load-bearing ("the model writes 'Pro' for the plan it read as 'pro'") — no test writes a recased restatement. |
| dict-**key** masking in `_mask` | The demo branch publishes the **whole raw `input` dict**, keys included and unclamped (only `arg_keys` is clamped). `{"Mia Torres": "x"}` → `{"[withheld]": "x"}` today; nothing holds it there. |
| allowlist-membership harvesting (`in _DEMO_RAW_TOOLS`) → hardcoded `!= "lookup_customer"` | Default-deny collapses. I confirmed the shipped behaviour is right: an unrecognised tool's result **is** harvested (`('THIRD-PARTY-VALUE',)`). Untested. |
| `missing_citations` masking | The one guardrail field carrying model-authored strings. No test feeds a third-party literal through it. |
| `_ordered`'s longest-first ordering | Alphabetical yields `[withheld] filed '[withheld] asks about SSO-INTERNAL-4417'` where the shipped order yields `[withheld] filed '[withheld]'` — a partial subject leak, because a subject can contain the name. |

Everything here is **correct in the shipped tree**. What is missing is the regression floor: five properties the fix's own docstrings argue for at length can each be deleted silently. Given that the reason this phase needed a second pass at all was "documented as guarded, not actually guarded", these deserve tests before the next change lands near them.

### NF-4 — the mask over-masks ordinary English (info)

`recent_tickets[].status` is harvested, and `open` / `resolved` are ordinary support-agent vocabulary. Live, from the anonymous route:

```
"Your ticket is still [withheld], and the earlier one is [withheld].
 I have opened an escalation and will process this promptly.
 Provide your invoice id. The problem is a duplicate charge."
```

`opened`, `process`, `Provide`, `problem` all survive correctly — the word boundary is doing its job. But `open` and `resolved` are replaced, on the surface that is the demo's payoff. This is the same legibility argument `_mask_pattern` makes for word boundaries, one level up: a status enum is not a disclosure, and harvesting it costs more than it protects. `recent_tickets[].status` could be excluded from collection with no loss.

---

## Did the mask gut DASH-03's content? **No.**

One live full-fidelity run driven through the real route (lookup → search_docs → a **denied** send_reply → an accepted send_reply), read back with no credential:

| DASH-03 element | Observed on the anonymous demo drill-down |
|---|---|
| step count | 17 ordered steps |
| **timings** | `elapsed_ms` present and `int` on **every** step; `duration_ms` computed on all four paired tool results |
| **tool inputs** | `arg_keys` on all four `tool_use` steps; raw `input` published for the three allowlisted tools (`search_docs`, `send_reply` ×2) with real values |
| **tool outputs** | `{"reply_id": 1, "status": "resolved"}`; the denial's own error envelope with `denied_by`, `expected_ticket_id`, `supplied_ticket_id` |
| **retrieval chunks with scores** | `[{"doc": "billing.md", "id": "billing.md#refunds", "score": 5.493061, "cited": true}]` |
| **cited-vs-not** | `cited: true`, through `normalise_citation` — the citation guard's own accept-set |
| **guardrail denials** | `{"guard": "ticket_binding", "tool": "send_reply", "action": "denied", "expected_ticket_id": 2, "supplied_ticket_id": 1001}` |
| KB text / doc ids / anchors masked? | **No** — `[withheld]` appears nowhere in the retrieval payload |
| costs / timings / counts masked? | **No** — non-strings are never collected, by construction |

The split the design describes is real and I confirmed it in the code path: `step["result"]` for an allowlisted tool is masked with the **route's** `withheld` (the address only), while `text` / tool `input` / `missing_citations` are masked with `prose_withheld` (route literal + run-derived). So the knowledge base, doc ids, scores, reply ids and statuses are untouched, and only model-composed fields carry the mask. The one masked prose line in that run — `"[withheld] on [withheld]. Per the docs I will reply."` — is masked exactly where the model restated the lookup and nowhere else.

## Did anything regress on the public / feed paths? **No.**

| Path | Check | Result |
|---|---|---|
| owner-origin `/runs/{uid}` | third-party name / subject present? | **no** |
| owner-origin `/runs/{uid}` | `"ticket"` key present? | **no** |
| owner-origin `/runs/{uid}` | `[withheld]` present anywhere? | **no** — the mask is built only on the branch that publishes prose, so the redacted branch gained no artefacts |
| owner-origin `/runs/{uid}` | published field names | `arg_keys, char_count, cost_usd, duration_ms, elapsed_ms, escalation_id, input_tokens, is_error, output_tokens, seq, status, steps, tool, type, unknown_arg_count, via` — unchanged from the first pass |
| `events.project()` (live feed) | `tool_result` for `lookup_customer` | `{"type": "tool_result", "tool": "lookup_customer", "is_error": false}` — unchanged |
| `events.project()` (live feed) | `text` frame | `{"type": "text"}` — no text field, unchanged |
| `/events` | any change | none; `project()` is not on the mask's call path at all |
| `tools.py` | `lookup_customer` `SELECT *` → named columns | confirmed: `SELECT email, name, plan, signed_up`. Strictly narrowing; the four columns are what the previous `dict(row)` returned. `evals.py` (frozen) calls this function and is unaffected — the payload shape is identical. |

---

## Observable Truths — first pass vs. this pass

| # | Truth (SC / requirement) | First pass | This pass | Evidence |
|---|---|---|---|---|
| 1 | **SC-1 / DASH-02** — aggregate cards + outcome distribution by SQL aggregation | VERIFIED | VERIFIED | unchanged; re-confirmed live (`/metrics` 200, 7 buckets) |
| 2 | **SC-2 / DASH-03** — drill-down shows tool inputs/outputs, timings, chunks + scores + cited-vs-not, guardrail denials | VERIFIED | **VERIFIED** | re-probed after the masking change — table above; all five elements intact |
| 3 | **DASH-03** — timings are real `elapsed_ms` | VERIFIED | VERIFIED | `elapsed_ms` int on all 17 steps; `duration_ms` on all pairs |
| 4 | **DASH-03** — cited-vs-not uses the citation guard's accept-set | VERIFIED | VERIFIED | `cited: true` via `normalise_citation` |
| 5 | **SC-3 / DASH-04** — inline SVG + budget gauge, no CDN, no build step | VERIFIED | VERIFIED | unchanged |
| 6 | **DASH-04 / WR-09** — p50 card and p50 chart are one population | VERIFIED | VERIFIED | unchanged |
| 7 | **DASH-04 / D-10** — daily series dense, ascending, bounded, empty-safe | VERIFIED | VERIFIED | unchanged |
| 8 | **SC-4 / DASH-05** — visitor submits a prefilled example with the demo key and watches it stream (server half) | VERIFIED | VERIFIED | unchanged |
| 9 | **D-04** — packaged template, no HTML literal in `main.py` | VERIFIED | VERIFIED | unchanged |
| 10 | **D-13** — guarded ALTERs safe against the live volume | VERIFIED | VERIFIED | unchanged |
| 11 | **SC-2 / D-02, CR-01** — the demo drill-down does not disclose a tool's output about anyone else | **FAILED** | **VERIFIED** | anonymous walk re-run: name, plan, date, other visitor's subject all masked; 7 mutations bind. Residuals NF-1/NF-2 recorded — same "floor, not proof" family as the already-accepted paraphrase vector |
| 12 | **SC-4 / DASH-05** — the Try-it interaction survives as wiring, not tokens | **FAILED (partial)** | **VERIFIED** | 8 binding deletions, 8 reds |
| — | **WR-10** — every frame field the page renders routes through `dash()` | **FAILED (partial)** | **VERIFIED** | 5 mutations red; full sweep of the six renderers shows no unguarded render site |
| 13 | Try-it deep link opens the submitter's own run **in a browser** | PRESENT_BEHAVIOR_UNVERIFIED | PRESENT_BEHAVIOR_UNVERIFIED | wiring now mutation-bound, but no DOM; checkpoint step 5 still unwitnessed |
| 14 | The demo-vs-owner fidelity contrast is visible **in a browser** | PRESENT_BEHAVIOR_UNVERIFIED | PRESENT_BEHAVIOR_UNVERIFIED | checkpoint step 6 still unwitnessed |

**Score: 12/12 counted must-haves verified** (was 9/12), with **2** truths present-and-wired but behaviour-unverified.

---

## Mutation ledger — this pass (25 mutations, 20 red / 5 green)

| # | Mutation | Verdict | Red test |
|---|---|---|---|
| M1 | delete `trySend.addEventListener("click", submitTryIt)` | RED | `test_try_it_controls_are_bound_to_their_handlers` |
| M2 | delete `chip.addEventListener("click", () => chooseExample(i))` | RED | same |
| M3 | delete `if (uid) offerTheTrace(uid);` | RED | same |
| M4 | delete `open.addEventListener("click", () => openDrill(uid))` | RED | + `test_try_it_deep_links_its_own_run` |
| M5 | delete `await runTryIt();` | RED | same |
| M6 | delete `await streamRun(ticket.id);` | RED | same |
| M7 | delete `tryActions.append(open);` | RED | same |
| M8 | delete `tryExamplesEl.append(chip);` | RED | same |
| M9 | unwrap `dash(s.tool)` in `renderStepBody` | RED | `test_no_step_describer_interpolates_a_raw_frame_field` |
| M10 | unwrap `dash(r.score)` to a bare `el()` arg | RED | same |
| M11 | unwrap the `STEP_LABELS[s.type]` fallback arm | RED | same |
| M12 | unwrap `dash(s.cause)` in the notice branch | RED | same |
| M13 | drop the `withheld_from_run(...)` term | RED | prose test + demo-branch field test |
| M14 | drop `authored=` at the route | RED | prose test |
| M15 | `_MIN_WITHHOLD_LEN = 4` | RED | prose test |
| M16 | mask without word boundaries | RED | prose test |
| M17 | **mask case-SENSITIVE** | **GREEN** | — → NF-3 |
| M18 | delete the route's `customer_email` literal | RED | missed-lookup test |
| M19 | **stop masking dict KEYS** | **GREEN** | — → NF-3 |
| M20 | **harvest by tool NAME instead of the allowlist** | **GREEN** | — → NF-3 |
| M21 | mask `text` but not tool `input` | RED | 2 tests |
| M22 | mask `input` but not `text` | RED | 4 tests |
| M23 | **don't mask `missing_citations`** | **GREEN** | — → NF-3 |
| M24 | **drop `_ordered`'s longest-first ordering** | **GREEN** | — → NF-3 |
| M25 | bare `d.note` (second `\|\|` render site) | RED | refusal-state test |

---

## Residual risk from the deferred structural fix, quantified

The structural closure — scoping `lookup_customer`'s `recent_tickets` so it stops handing the model other visitors' subjects — was deferred because `evals.py` is frozen for this phase. **I checked the coupling rather than accepting the claim:** `src/relay/evals.py:357-363` calls `lookup_customer(conn, case["customer_email"])` directly and feeds the whole payload to the LLM judge, so scoping the query needs a new parameter at that call site, which needs `evals.py` to change. **The deferral is grounded in a real coupling, not an excuse.** It also cost nothing this phase: the mask closes the literal vector regardless, and the narrowing that *was* possible inside the freeze (`SELECT *` → four named columns) was taken.

What the residual actually is, bounded as fairly as I can:

| Dimension | Extent |
|---|---|
| **Vector** | model **paraphrase** only. Verbatim restatement — the common case for a quoted subject line ("she also wrote in about 'X'") — is masked. Paraphrase of a subject's *gist* is not. |
| **Reachable victims** | the **4 seeded fictional identities** only. `lookup_customer` returns `{"found": false}` for any address without a `customers` row and no route inserts one, so there is no pivot to an arbitrary address. Three of the four are pinned by the Try-it chips; the fourth needs a hand-rolled POST with the published demo key. |
| **Values at risk** | (a) name and plan — both **literal constants in `src/relay/db.py` in a public repo**, so disclosing them discloses nothing; (b) up to 10 **ticket subject lines** filed against that address. |
| **Whose subjects** | other demo visitors' (the chips file identical subjects, so those are public by construction — the residual is only the ones a visitor **edited**), plus owner-filed (`scripts/demo.sh`) and eval-filed subjects. |
| **Not at risk** | ticket **bodies** (`recent_tickets` selects `id, subject, status, created_at` only), email addresses (masked by the route literal, and now also run-derived), any real person's data. |
| **Window** | a subject stays in the lookup until 10 newer tickets exist for that address (`ORDER BY created_at DESC LIMIT 10`). Drill-down steps are swept at 30 days; the `tickets` row is not. |
| **Attacker cost** | needs a demo run to complete against the same seeded address, then a keyless `GET /metrics` → `GET /runs/{uid}`, and then needs the model to have paraphrased rather than quoted. |

**My assessment: LOW, and appropriately deferred.** The disclosure class that had real teeth — verbatim third-party subject lines on a keyless, 30-day, anonymously-enumerable route — is closed. What is left is gist-level leakage of visitor-edited subject lines against four fictional accounts, gated on the model choosing to paraphrase. NF-1 and NF-2 raise the residual a little (a bare first name escapes; a guess can be confirmed), and both are cheap to close inside the existing contract. The one thing I would not do is let the docstrings' current confidence stand: they claim more than NF-1/NF-2 support.

---

## Requirements Coverage — the explicit verdicts

| Req | First pass | **This pass** | Basis |
|---|---|---|---|
| **DASH-02** | ✓ complete | ✓ complete (unchanged) | already recorded in REQUIREMENTS.md |
| **DASH-03** | ⚠️ do not mark complete | ✓ **YES — mark complete** | The only thing blocking it was CR-01's prose residual, which I re-attacked anonymously and could not reproduce. All five named elements (tool inputs, outputs, real `elapsed_ms`/`duration_ms` timings, retrieval chunks with scores, cited-vs-not through the guard's own accept-set, guardrail denials) are delivered on a live run **after** the masking change and are mutation-bound. NF-1..NF-4 are warnings on the same disclosure surface, none of which re-opens the demonstrated attack or removes a DASH-03 element. |
| **DASH-04** | ✓ complete | ✓ complete (unchanged) | already recorded in REQUIREMENTS.md |
| **DASH-05** | ⚠️ do not mark complete | ✓ **YES — mark complete, under the standard already applied to DASH-02/DASH-04** | The blocker was silent-removability, and it is gone: eight independent binding deletions all red, with the chain and three positions asserted. What remains — that a browser fires the handlers — is the same class of unwitnessed presentation that DASH-02 and DASH-04 were marked complete over, and it is already tracked as the **phase-level** human checkpoint (steps 5 and 6), not as a DASH-05-specific defect. If the project's bar is instead "no requirement is complete until its checkpoint is witnessed", then DASH-04 must be reopened too; I do not recommend that. **Mark DASH-05 complete and keep checkpoint steps 5/6 open as the phase's human gate.** |

No orphaned requirements: `REQUIREMENTS.md` maps exactly DASH-02..05 to Phase 6 and all four are claimed by plans.

---

## Anti-Patterns

| File | Pattern | Severity | Note |
|---|---|---|---|
| — | `TBD`/`FIXME`/`XXX` in phase-6 source | ℹ️ none | Only the same false positives as the first pass (`\uXXXX` in two docstrings, "placeholder" naming the `dash()` helper). No unreferenced debt markers. |
| `src/relay/events.py` | `withheld_from_run` / `mask_withheld` docstrings assert controls that no test holds | ⚠️ Warning | NF-3 — five properties, all mutation-green |
| `src/relay/events.py` | `withheld_from_run` docstring asserts `authored` "cannot" be used to unmask | ⚠️ Warning | NF-2 — it can, as a confirmation oracle |
| `src/relay/events.py` | LIMITS block omits the sub-token / whitespace-variant limit | ⚠️ Warning | NF-1 |
| `src/relay/events.py` | status enum harvested into the mask | ℹ️ Info | NF-4 — legibility cost on the payoff surface |

No stubs, no placeholder returns, no hollow props. Data-flow trace unchanged from the first pass: every rendered value still terminates in a real query (re-confirmed for the drill-down and retrieval paths after the masking change).

---

## Known-open, carried forward unchanged (NOT counted as failures)

WR-01, WR-04, WR-03's substance; Phase 5's WR-03/WR-08/WR-11/WR-12 and W-3; the paraphrase vector and the deferred `recent_tickets` scoping (quantified above). All were accepted before this pass and none moved.

---

## Gaps Summary

**No gaps remain.** All three findings that blocked the first pass are closed, and I closed each verdict on evidence I produced myself — the anonymous attack re-run end to end, and twenty-five mutations of which twenty red the test they should have.

What stops this being `passed` is not a defect: it is that **two truths are present and wired but behaviourally unwitnessed**, because this suite has no DOM and 06-07's human checkpoint steps 5 and 6 were never performed. That routes the phase to `human_needed`, which is where it should have been from the start on those two rows.

Four new findings are recorded as warnings, not blockers. Three of them (NF-1, NF-2, NF-3) share one shape, and it is worth naming because this phase keeps producing it: **a control that is correct in the shipped tree and stated more confidently in its docstring than its tests support**. CR-02 was that. The first pass's "proven on one of two vectors, documented as proven on both" was that. NF-3's five mutation-green properties are that again. The code is right; the claims run ahead of the guards. The cheapest durable fix is to hold each docstring assertion to a test before the next change lands near `events.py`.

**Recommendation:** mark **DASH-03 and DASH-05 complete**. Book NF-1..NF-4 as follow-up work on the disclosure surface — NF-2's narrower `authored` exemption and NF-3's five regression tests first, since both are small and both defend claims already written down. Perform checkpoint steps 5 and 6 in a browser before the phase is signed off.

---

*Verified: 2026-08-14T08:40:00Z (second pass, at `4edea40`)*
*Verifier: Claude (gsd-verifier)*
*Working tree restored; `pytest -q` = **420 passed**, `ruff check src tests` clean, `git status --short` empty at finish.*
