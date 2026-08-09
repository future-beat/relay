# Phase 1: Security Perimeter - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-08-06
**Phase:** 01-security-perimeter
**Areas discussed:** Key & demo-key policy, Limit & budget values, Public vs protected surface, Injection denial behavior

---

## Key & demo-key policy

| Option | Description | Selected |
|--------|-------------|----------|
| Published in README + dashboard | Zero-friction; the key is rate-limited anyway, openness is the portfolio statement | ✓ |
| /demo-key endpoint | Fetched from an endpoint with its own rate limit | |
| Baked into Try-it form only | Dashboard uses it invisibly; curl users find it in README | |

| Option | Description | Selected |
|--------|-------------|----------|
| Two env vars | RELAY_API_KEY (owner) + RELAY_DEMO_KEY (demo) — explicit tiers | ✓ |
| One var, tier-prefixed list | RELAY_API_KEYS="owner:xxx,demo:yyy" — extensible, more parsing | |

---

## Limit & budget values

| Option | Description | Selected |
|--------|-------------|----------|
| $5/day ceiling | ~40–100 runs/day, worst case ~$150/month | ✓ |
| $2/day | Tighter; may hit breaker during a reviewer's session | |
| $10/day | Generous, worst case ~$300/month | |

| Option | Description | Selected |
|--------|-------------|----------|
| Demo: 5 runs/hour per IP | Several tries per session, throttles scripted abuse; 20/hour ticket creation | ✓ |
| 3 runs/hour | Stingier | |
| 10 runs/hour | Looser, leans on the daily breaker | |

| Option | Description | Selected |
|--------|-------------|----------|
| Owner: loose ceiling (~60 runs/hour) | Protects against leaked key, never blocks legit use | ✓ |
| Unlimited | Only the daily budget applies | |

---

## Public vs protected surface

| Option | Description | Selected |
|--------|-------------|----------|
| Dashboard, metrics, health public | GET /tickets/{id} requires a key | ✓ |
| Everything read-only public | GET /tickets/{id} public too | |
| Only health + dashboard public | /metrics also keyed | |

| Option | Description | Selected |
|--------|-------------|----------|
| Friendly JSON with reset info | 429/503 bodies explain the limit, reset time, cost-control framing | ✓ |
| Terse standard errors | Minimal detail strings | |

---

## Injection denial behavior

| Option | Description | Selected |
|--------|-------------|----------|
| Deny + let agent continue | Agent can self-correct within step budget | ✓ |
| Deny + terminate run | Mismatch immediately ends the run | |

| Option | Description | Selected |
|--------|-------------|----------|
| Reject with visible denial | Model-readable refusal; observable rejection is the demo artifact | ✓ |
| Silently override to correct id | Safest but invisible | |

| Option | Description | Selected |
|--------|-------------|----------|
| Distinct event type | Dedicated `guardrail` SSE event + log counter | ✓ |
| Regular tool_result error | Rides existing shape, less visible | |

## Claude's Discretion

- Rate-limit strategy/library wiring (research recommends `limits>=5.8` as route dependencies)
- Constant-time compare implementation, 401/403 wiring details
- MCP default-flip documentation/migration
- Test structure following existing conventions

## Deferred Ideas

- Per-key usage accounting on the dashboard — Phase 5/6 / v2 (rejected-action counter)
- `/demo-key` endpoint variant — rejected in favor of open publication
