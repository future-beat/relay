---
phase: quick-261023-pqt
plan: 01
subsystem: infra
tags: [fly.io, health-check, ops, deploy]
status: complete
requires: []
provides:
  - "Fly.io HTTP health check on GET /health (interval 30s, timeout 5s, grace 10s)"
affects:
  - fly.toml
tech-stack:
  added: []
  patterns:
    - "Why-explaining comment directly above each fly.toml setting"
key-files:
  created: []
  modified:
    - fly.toml
decisions:
  - "Health check timeout is 5s, not the Docker HEALTHCHECK's 3s: it matches busy_timeout, which bounds the measured ~0.81s loop stall under the DB lock (plus shared-cpu jitter)"
  - "grace_period is 10s to cover the ~5s cold start from stopped"
metrics:
  duration: "~3 min"
  completed: 2026-10-23
actuals:
  tokens: 300
  tasks: 1
  commits: 1
---

# Phase quick-261023-pqt Plan 01: Fly.io HTTP Health Check Summary

fly.toml now has a single `[[http_service.checks]]` block. It sends GET /health every 30s with a 5s timeout and a 10s grace period. A comment above it explains each value: /health costs nothing to probe, 5s matches the busy_timeout bound on the DB-lock stall, 10s covers a cold start, a failing check stops routing and fails the deploy but does not restart the machine, and checks do not keep the machine from scaling to zero.

## Tasks

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 | Add commented [[http_service.checks]] block for GET /health | 626baf8 | fly.toml |

## Verification

The plan's automated verify command passed:

```
Validating /Users/hesamabbaszadi/Documents/Projects/Relay/fly.toml
✓ Configuration is valid

checks block ok; all other config identical to main
VERIFY-PASS
```

- `git diff --numstat main -- fly.toml`: `16	0	fly.toml` (additions only)
- `git show --stat HEAD`: only fly.toml, subject `ops(fly): add HTTP health check on /health`
- Before inserting the comment, I checked its facts against the code: `/health` at src/relay/main.py:237-239 (in-memory only), the 0.81s stall comment at src/relay/main.py:170-175, `PRAGMA busy_timeout = 5000` at src/relay/db.py:235, and `HEALTHCHECK --timeout=3s` at Dockerfile:20.
- The only fly command run was `fly config validate`. Nothing was deployed, scaled, or changed in secrets.

## Deviations from Plan

None. The plan was executed exactly as written, and the reference block was inserted word for word.

## Out of Scope (orchestrator-owned, L5)

- `fly deploy`, then `fly checks list` / `fly status` to see the check pass on the live machine
- Confirming the machine still auto-stops when idle with the check in place

## Self-Check: PASSED

- FOUND: fly.toml contains `[[http_service.checks]]` with `path = '/health'`
- FOUND: commit 626baf8
