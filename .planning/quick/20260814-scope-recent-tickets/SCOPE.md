---
slug: scope-recent-tickets
date: 2026-08-14
status: scoped
---

# Scope: stop `lookup_customer` putting other people's words in the model's context

## The problem, precisely

`tools.py:lookup_customer` returns `recent_tickets` — the last 10 tickets for an
address, `WHERE customer_email = ?` with no other predicate. The Try-it form pins each
example to a **seeded** customer and lets the visitor **edit the subject** before
submitting (`dashboard.html:112`, a free-text input). So `recent_tickets` for
`mia@datalane.ai` accumulates **every demo visitor's typed subject**, and every one of
those is handed to the model, restated into prose, and published on the keyless
`GET /runs/{uid}` for any demo run against that address.

The `[withheld]` mask (PR #10) is a literal filter over that prose. Re-verification found
it misses `Hi Mia,` when the literal is `Mia Torres` (NF-1), and that `authored` lets a
visitor unmask a value by naming it (NF-2). Both are symptoms. The disease is that
someone else's free text is in the context at all.

## The key observation

**`subject` is the only free-text field in the payload.**

```
customer:       email, name, plan, signed_up   -> enumerable / fictional constants
recent_tickets: id, subject, status, created_at
                     ^^^^^^^ the only field a stranger can type into
```

`SEED_CUSTOMERS` (`db.py:90-95`) are four fictional personas hardcoded in a public
repo — `Mia Torres`, `pro`, `2024-08-30` are not secrets and never were. `status` is one
of a closed set. `id` and `created_at` are enumerable. So the entire disclosure risk
rides on one field, and removing it removes the class rather than filtering it.

## What the model actually needs history for

The system prompt (`prompts.py:1-30`) uses history for exactly one decision: escalate
when "the customer is clearly frustrated or at risk of leaving." The signal is
**how many tickets, how recent, how many unresolved** — never the text. Nothing in the
prompt asks the model to quote a prior subject, and the reply is supposed to address the
ticket under review.

So the signal survives the fix. That is what makes this cheap.

## Proposed change

Drop `subject` from `recent_tickets`. Keep `id`, `status`, `created_at`, and select
named columns rather than `SELECT *` on `tickets` as well (`lookup_customer` was already
tightened this way on `customers` in PR #10).

Consider additionally collapsing to an aggregate (`{count, open, last_filed}`) — smaller
context, same signal, and no per-row ids. Recommend deciding this against the eval
result rather than up front: the list form is the smaller change and the smaller risk.

### Second-order consequence, worth taking in the same pass

With no third-party free text in the context, the mask's remaining job is the *seeded
persona's* name and plan — public constants. That reopens two of the four findings
cheaply:

- **NF-4 (over-masking)** — the harvest currently takes `recent_tickets[].status`, so
  "open" and "resolved" render as `[withheld]` in the demo's payoff prose. Once
  `subject` is gone, the harvest should take **free-text fields only**, never
  enumerable ones. That fixes NF-4 outright.
- **NF-1 / NF-2** — their blast radius shrinks to fictional constants. They should still
  be fixed (a sub-token miss and a visitor-controlled oracle are wrong regardless), but
  they stop being disclosure risks and become correctness bugs.

**Do not remove the mask.** It is the defence-in-depth layer for whatever a future tool
returns; `_DEMO_RAW_TOOLS` is default-deny for exactly that reason.

## The coupling that made this a deferral

`evals.py:357-363` calls `lookup_customer(conn, case["customer_email"])` directly and
feeds the payload to the LLM judge, commented "give the judge exactly what the agent
could see". Changing the payload changes the judge's context, so it can change grading
independently of any change in agent behaviour. `evals.py` was frozen through Phase 6;
Phase 6 is closed, so it can be unfrozen — but the change must be **measured, not
assumed**.

## How to measure it

1. Baseline: run the eval suite on `main` unchanged, keep the report.
2. Apply the change (including the matching update in `evals.py`).
3. Re-run with the same limit/threshold; diff pass rate, per-case verdicts and judge
   quality scores.
4. Watch the **escalation cases specifically** — they are the ones that consume the
   history signal. A silent drop there is the real risk, not the aggregate pass rate.

Reference points: Phase 4 closed at 12/12, quality 5.0. Per-run cost on the dashboard is
~$0.038, so two full passes over ~12 cases plus judge calls should land well under $2.
This spends real Anthropic credit and needs explicit go-ahead.

## Risks

- **Grading moves for a reason unrelated to agent quality.** The judge loses context it
  currently has. Mitigated by measuring both directions and reading per-case diffs, not
  just the headline.
- **Escalation recall drops** if the model was leaning on subject text rather than
  counts. This is the outcome worth watching; if it appears, the aggregate form (with an
  explicit `open` count) is the fix rather than reverting.
- **Frozen-file discipline.** `evals.py` moves in this change. It should be named as
  in-scope up front rather than edited quietly.

## Not in scope

- NF-1 and NF-2's own fixes (sub-token matching, the `authored` oracle) — smaller and
  independent; can ride along or follow.
- Any change to `_DEMO_RAW_TOOLS`, the public branch, or `project()`.
- The Phase 5/6 deferred set (WR-01, WR-04, WR-03 substance).

## Shape

Three tasks, one wave, plus a measurement gate that costs money:

1. `lookup_customer` drops `subject`, selects named columns on `tickets`; unit test that
   no free text is returned, mutation = restore the column.
2. Harvest free-text fields only; NF-4 regression test (`status` words survive in prose).
3. Update `evals.py`'s judge context to match, then the before/after eval run as an
   explicit checkpoint with the diff reported.

Suite floor 420. The security assertion that must still hold at the end: the anonymous
walk (`/metrics` -> uid -> `/runs/{uid}`) returns no other visitor's typed text — which
should now be true *structurally*, not because a mask caught it.
