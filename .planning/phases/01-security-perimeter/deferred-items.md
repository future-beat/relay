# Deferred items — Phase 01 security perimeter

Out-of-scope discoveries logged during execution. Not fixed in this phase.

## README quotes a stale test count

- **Found during:** plan 01-05, Task 3 (editing the Deployment section)
- **Where:** `README.md`, Deployment section — "CI runs lint, the 37-test suite, and a Docker build"
- **Issue:** the suite is 97 tests after this phase. The number was already stale before this
  phase touched anything, and it will go stale again every phase that adds tests.
- **Suggested fix:** drop the hardcoded count rather than update it ("CI runs lint, the test
  suite, and a Docker build"). Deliberately not changed here — it is unrelated to the perimeter
  and the plan's acceptance criteria pin the README diff to security content.
