---
phase: quick-261023-pqt
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - fly.toml
autonomous: true
requirements:
  - QUICK-261023-pqt

estimate:
  tokens: 15000
  raw_tokens: 15000
  tasks: 1
  confidence: low

must_haves:
  truths:
    - "fly.toml declares exactly one HTTP check under http_service: GET /health, interval 30s, timeout 5s, grace_period 10s (L2)"
    - "Every other fly.toml setting parses identical to main: [[vm]] shared-cpu-1x/512mb, primary_region syd, min_machines_running 0, auto_stop_machines 'stop', auto_start_machines true, [[mounts]], [env], kill_timeout 30 (L1)"
    - "fly config validate passes from the repo root (L4)"
    - "A comment directly above the check block explains why: /health is free to probe; 5s timeout vs the Dockerfile HEALTHCHECK's 3s (0.81s lock stall, shared-cpu jitter, busy_timeout 5s); 10s grace covers the ~5s cold start; a failing check stops routing and gates fly deploy but does not restart the machine (restart policy owns crash/OOM recovery); checks are not proxied traffic so they never keep the machine awake (L3)"
    - "No source code, Dockerfile, or secret changes: the only non-.planning file differing from main is fly.toml, and that diff is additions only (L1, L4)"
  artifacts:
    - path: "fly.toml"
      provides: "Fly.io HTTP health check on /health with rationale comment"
      contains: "[[http_service.checks]]"
  key_links:
    - from: "fly.toml [[http_service.checks]]"
      to: "src/relay/main.py @app.get(\"/health\")"
      via: "path = '/health' on internal_port 8080"
      pattern: "path = '/health'"
---

<objective>
Add a Fly.io HTTP health check that probes GET /health to fly.toml, with a short comment
explaining why each value was chosen. Infra-only: one block plus its comment, nothing else.

Locked decisions (from the orchestrator, verified, not to be revisited):
- L1: Infra-only change to fly.toml. Do not change [[vm]] (shared-cpu-1x, 512mb),
  primary_region (syd), min_machines_running (0), auto_stop_machines ('stop'),
  auto_start_machines (true), [[mounts]], [env], kill_timeout, or any secrets. No source
  code changes.
- L2: Add a [[http_service.checks]] block: method GET, path "/health", interval "30s",
  timeout "5s", grace_period "10s".
- L3: A short comment above the block, in the file's existing why-explaining comment style,
  covering: /health is public, unmetered, no DB/API work (src/relay/main.py:237); 5s timeout
  rather than the Docker HEALTHCHECK's 3s because of the measured ~0.81s event-loop stall
  under the DB lock (src/relay/main.py ~170-178) plus shared-cpu jitter, and 5s matches
  busy_timeout; 10s grace covers a cold start (~5s observed wake from stopped); Fly does not
  restart a machine on a failing check, it only stops routing to it and gates fly deploy,
  while crash/OOM recovery comes from the machine restart policy (default on-failure);
  checks are not proxied traffic, so they do not keep the scale-to-zero machine awake.
- L4: Single task. Verification: fly config validate passes (from repo root) and the diff
  shows only the added block + comment in fly.toml.
- L5: Deploy and live verification are owned by the orchestrator after execution and are
  NOT part of this plan. Do not run fly deploy, fly secrets, or any fly command that mutates
  the app.

Purpose: Fly currently has no health signal for relay-agent (Fly Machines do not run the
Dockerfile HEALTHCHECK), so a wedged machine keeps receiving traffic and a bad image can
roll out unchecked. A check on the free /health endpoint stops routing to an unhealthy
machine and gates fly deploy, without costing anything or defeating scale-to-zero.

Output: fly.toml with one new commented [[http_service.checks]] block; one commit.

Tracer note: this is a quick-mode, single-file config change, so the lone task is its own
end-to-end slice. Its local end is fly config validate plus a parsed-TOML comparison
against main; the live end (deploy, then watching the check go passing) belongs to the
orchestrator per L5.
</objective>

<execution_context>
@$HOME/.claude/gsd-core/workflows/execute-plan.md
@$HOME/.claude/gsd-core/templates/summary.md
</execution_context>

<context>
@CLAUDE.md
@fly.toml

Facts already confirmed during planning (do not re-derive):
- HEAD == main == 7950e1d; fly.toml is unchanged from main, so main:fly.toml is the
  baseline for every diff check below.
- fly.toml currently has no checks; [http_service] ends at line 36
  (min_machines_running = 0), followed by a blank line and [[vm]] at line 38.
- src/relay/main.py:237-238 is the /health route; it returns status, version and model
  from in-memory settings only (no DB, no Claude API call).
- src/relay/db.py:235 sets PRAGMA busy_timeout = 5000; src/relay/main.py:170-175 documents
  the measured 0.81s loop stall bounded by busy_timeout.
- Dockerfile line 20: HEALTHCHECK --interval=30s --timeout=3s --start-period=5s.
- fly CLI is installed at /opt/homebrew/bin/fly, and fly config validate works locally
  (it passes on the current file).
- The reference block below was dry-run in a scratch copy: fly config validate passes,
  tomllib parses it to exactly the L2 values, and every comment line is at most 88
  characters, matching the file's existing wrap width.

Reference block for Task 1. Insert it verbatim. The leading blank line separates it from
min_machines_running; the existing blank line before [[vm]] stays.

```toml

# Probes /health because it is public, unmetered and does no DB or Claude API work, so
# a check every 30s costs nothing. timeout is 5s, not the Dockerfile HEALTHCHECK's 3s:
# a worker holding the DB lock has stalled the event loop for a measured ~0.81s,
# shared-cpu jitter stacks on top, and 5s is busy_timeout, the bound on that stall.
# grace_period covers a cold start, observed at ~5s to wake from stopped. A failing
# check does not restart the machine: Fly only stops routing to it and fails
# `fly deploy`. Crash and OOM recovery is the restart policy's job (default
# on-failure). Checks are not proxied traffic, so they never keep the scale-to-zero
# machine awake.
[[http_service.checks]]
  method = 'GET'
  path = '/health'
  interval = '30s'
  timeout = '5s'
  grace_period = '10s'
```
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add the commented [[http_service.checks]] block for GET /health to fly.toml</name>
  <files>fly.toml</files>
  <read_first>
    - fly.toml (whole file, 40 lines; note the comment style and 2-space key indentation)
  </read_first>
  <action>
Using the Edit tool, insert the reference block from the context section into fly.toml
directly after the line "  min_machines_running = 0" (the last key of [http_service]) and
before the existing blank line that precedes [[vm]]. This implements L2 (the five check
values: method GET, path /health, interval 30s, timeout 5s, grace_period 10s) and L3 (the
why-comment). Match the file's conventions: the comment lines and the
[[http_service.checks]] header start at column 0, the five keys are indented two spaces,
string values use single quotes, and comment lines wrap at 88 characters or fewer.

The header must be [[http_service.checks]] (double brackets, an array of tables nested
under http_service). It must come after the [http_service] keys. A bare key placed after
that header is parsed into the check table, which is the same trap the existing
kill_timeout comment warns about.

Use the reference comment wording verbatim. Every fact in it was confirmed during
planning. If you find any fact in it to be wrong, stop and report it rather than
rewording it yourself.

Per L1, change nothing else in fly.toml: no edits, reorders, or reformatting of [[vm]],
primary_region, min_machines_running, auto_stop_machines, auto_start_machines,
[[mounts]], [env], [build], kill_timeout, or the existing comments. Per L1, touch no
other file: not src/, not the Dockerfile, not secrets. Per L5, run no fly command
other than fly config validate. No deploy, no secrets, no scale, nothing that mutates
the app.

After the verify command passes, commit only fly.toml: stage it by explicit path, not
with git add -A or git add ., then commit with this message (subject, blank line,
trailer):
  subject line: ops(fly): add HTTP health check on /health
  trailer line: Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  </action>
  <verify>
    <automated>cd /Users/hesamabbaszadi/Documents/Projects/Relay && fly config validate && python3 -c "import tomllib,subprocess,sys; new=tomllib.load(open(sys.argv[1],'rb')); old=tomllib.loads(subprocess.check_output(['git','show','main:fly.toml'],text=True)); c=new['http_service'].pop('checks'); assert c==[{'method':'GET','path':'/health','interval':'30s','timeout':'5s','grace_period':'10s'}], c; assert new==old, 'config outside the checks block drifted from main'; print('checks block ok; all other config identical to main')" fly.toml && git diff --numstat main -- fly.toml | awk '$2!=0{print "lines removed from fly.toml: " $0; exit 1}' && test "$(git diff --name-only main -- . ':(exclude).planning')" = "fly.toml" && grep -q 'busy_timeout' fly.toml && grep -q '0.81s' fly.toml && grep -q 'cold start' fly.toml && grep -q 'restart policy' fly.toml && grep -q 'scale-to-zero' fly.toml && awk 'NR>5 && length($0)>88{print "line " NR " exceeds 88 chars"; bad=1} END{exit bad}' fly.toml && echo VERIFY-PASS</automated>
  </verify>
  <done>
- fly config validate prints "Configuration is valid" from the repo root (L4).
- Parsed with tomllib, fly.toml has http_service.checks equal to exactly one table:
  method GET, path /health, interval 30s, timeout 5s, grace_period 10s (L2). With that key
  removed, the parsed config equals main:fly.toml, so vm, region, scale-to-zero settings,
  mounts, env and kill_timeout are all unchanged (L1).
- git diff --numstat main -- fly.toml shows 0 deleted lines (additions only), and fly.toml
  is the only non-.planning path that differs from main (L1, L4).
- The comment above the block covers all five L3 rationale points (busy_timeout, 0.81s,
  cold start, restart policy, scale-to-zero all present), and no new line is longer than
  88 characters.
- One commit exists containing only fly.toml, with subject
  "ops(fly): add HTTP health check on /health" and the Co-Authored-By trailer.
- No fly command other than fly config validate was run (L5).
  </done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| Fly health checker -> machine :8080/health | Fly's internal checker probes the app directly. This is not proxied user traffic. |
| Public internet -> /health (pre-existing) | /health is already public and unauthenticated. This change adds no new route or exposure. |
| Repo fly.toml -> Fly platform config | fly.toml is the deploy-time source of truth. An accidental edit to another key changes production behavior. |

## STRIDE Threat Register

| Threat ID | Category | Component | Severity | Disposition | Mitigation Plan |
|-----------|----------|-----------|----------|-------------|-----------------|
| T-pqt-01 | Denial of Service | [[http_service.checks]] timeout/grace | medium | mitigate | A timeout that is too tight would mark a healthy but briefly stalled machine as failing, so Fly stops routing to it and deploys fail. Mitigated by timeout 5s, which equals the busy_timeout bound on the measured 0.81s lock stall plus jitter, and grace_period 10s, which covers the ~5s cold start. Values are asserted exactly in the verify command. |
| T-pqt-02 | Tampering | fly.toml keys outside the new block | medium | mitigate | An accidental edit to min_machines_running, [[vm]], mounts or env would silently change cost, durability or the trust-proxy setting in production. Mitigated by the verify step: it parses fly.toml with tomllib, removes only http_service.checks, and asserts the result equals main:fly.toml; numstat must show zero deleted lines. |
| T-pqt-03 | Denial of Service (cost) | Check traffic vs scale-to-zero | low | accept | Fly health checks are not proxied traffic and do not count toward auto-stop idleness, so a 30s probe does not keep the machine awake. The orchestrator's post-deploy live check (L5) is where this is observed in production. |
| T-pqt-04 | Information Disclosure | /health response (version, model) | low | accept | This exposure already exists and is intentional (a public liveness endpoint). The check reads the same response and adds no data and no route. |
| T-pqt-05 | Elevation of Privilege | fly CLI during execution | low | mitigate | The executor may run only the read-only fly config validate command (L5). Deploys and secret changes stay with the orchestrator, so this plan cannot mutate the live app. |
</threat_model>

<verification>
- The Task 1 automated verify command prints VERIFY-PASS.
- git show --stat HEAD lists only fly.toml, and the subject is
  "ops(fly): add HTTP health check on /health".
- Out of scope here (owned by the orchestrator per L5): fly deploy, then fly checks list /
  fly status showing the http check as passing, and confirming the machine still
  auto-stops when idle.
</verification>

<success_criteria>
- fly.toml has exactly one [[http_service.checks]] (GET /health, 30s / 5s / 10s) with a
  why-comment covering all L3 points.
- fly config validate passes.
- Parsed config is otherwise identical to main, the diff is additions only, and only
  fly.toml differs outside .planning.
- One atomic commit, ops(fly): add HTTP health check on /health, with the Co-Authored-By
  trailer.
</success_criteria>

<output>
Create `.planning/quick/261023-pqt-add-fly-io-http-health-check-on-health-t/261023-pqt-SUMMARY.md` when done
</output>
