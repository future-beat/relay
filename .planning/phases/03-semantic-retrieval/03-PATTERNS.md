# Phase 3: Semantic Retrieval - Pattern Map

**Mapped:** 2026-08-10
**Files analyzed:** 15 (5 new, 8 modified, 2 no-op/confirm)
**Analogs found:** 13 / 15 (2 no-analog: outbound-HTTP client, standalone build script)

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/relay/retrieval.py` (NEW) | service/utility | transform + request-response | `src/relay/tools.py` (module shape) + `src/relay/config.py:22` (key aliasing) | role-match (no outbound-HTTP analog) |
| `scripts/build_index.py` (NEW) | script | batch / file-I/O | RESEARCH Code Example 1 (no Python-script analog in repo) | no-analog |
| `kb/index.json` (NEW) | config/artifact | file-I/O | RESEARCH Pattern 1 shape (committed artifact) | no-analog |
| `tests/test_retrieval.py` (NEW) | test | unit | `tests/test_tools.py` (direct executor calls) | exact |
| `tests/test_index.py` (NEW) | test | unit | RESEARCH Code Example 5 + `tests/conftest.py:42` KB fixture | role-match |
| `src/relay/tools.py` (MOD) | tool registry | CRUD + transform | itself (`search_docs` L48-64, `send_reply` L79-87, `build_registry` L96-219) | exact |
| `src/relay/agent.py` (MOD) | agent loop | event-driven | itself (`bind_to_ticket` L52-72, `_execute_guarded` L75-129, binding-event branch L250-293) | exact |
| `src/relay/guardrails.py` (MOD) | model/validation | validation | itself (`SendReplyInput` L28-30, `SearchDocsInput` L19-20) | exact |
| `src/relay/config.py` (MOD) | config | — | itself (`anthropic_api_key` L22 `validation_alias`) | exact |
| `src/relay/models.py` (MOD) | model | — | itself (`AgentEvent.type` comment L41) | exact |
| `src/relay/prompts.py` (MOD) | prompt | — | itself (`SYSTEM_PROMPT` L1-26) | exact |
| `pyproject.toml` (MOD) | config | — | itself (`dependencies` L9-19) | exact |
| `.env.example` (MOD) | config | — | itself (`ANTHROPIC_API_KEY` line) | exact |
| `tests/test_tools.py` (MOD) | test | unit | itself (L15-56 result-shape + execute calls) | exact |
| `tests/test_guardrails.py` (MOD) | test | unit | itself (binding tests L220-309) | exact |
| `Dockerfile` | — | — | no edit needed — `COPY kb ./kb` (Dockerfile:10) already ships `kb/index.json` | — |

---

## Pattern Assignments

### `src/relay/retrieval.py` (NEW — service/utility, transform + request-response)

**Analog:** `src/relay/tools.py` for module conventions; `src/relay/config.py:22` for the Voyage key; `src/relay/agent.py:126-129` for the sanctioned narrow degrade-catch. There is **no existing outbound-HTTP client** in the codebase (all HTTP goes through the Anthropic SDK), so the `httpx.post` body itself follows RESEARCH Code Example 2 — but the *shape* (module docstring stating the phase/why, `_`-prefixed helpers, keyword-only past 2-3 args, sync executor) copies `tools.py`.

**Module docstring pattern** (copy the "why, referencing phase" style — `tools.py:1-5`):
```python
"""Agent tools: Claude-facing schemas plus their local executors.

Each tool carries a permission tier ("read" or "write") so phase 2 can gate
write actions behind confirmation or policy without touching the loop.
"""
```

**Degrade-never-raise catch** — this is the ONE new narrow catch this phase adds; model it on the single sanctioned broad-except at `agent.py:126-129`, which is `# noqa: BLE001` justified inline:
```python
    try:
        return spec.execute(**validated), False
    except Exception as exc:  # noqa: BLE001 — surfaced to the model, not swallowed
        return json.dumps({"error": str(exc)}), True
```
For retrieval the equivalent is: catch Voyage failure, return `None`/keyword-fallback, set `degraded=True` — never let it end the run (RESEARCH Code Example 2 shows the `# noqa: BLE001 — degrade, never end the run` form).

**Key access** — read via settings, never re-read env; the key comes from `config.py` (see config assignment below). Pass explicitly into the request headers.

**Load-once seam** — `load_index(kb_dir)` returns an in-memory normalized matrix + meta; `build_registry` calls it once and the `search_docs` closure captures it, exactly as `build_registry` already captures `conn`/`kb_dir` (`tools.py:118,140,168,191,215-217`). No per-call disk read.

---

### `scripts/build_index.py` (NEW — script, batch/file-I/O)

**Analog:** NONE in repo — `scripts/` holds only `demo.sh` (bash). Follow RESEARCH Code Example 1 verbatim (offline builder, `input_type="document"`, `kb_sha256` stamp). Reuse the SAME `kb_sha256` and `headings` helpers that `retrieval.py` exports so the builder and the CI gate cannot drift (RESEARCH Pattern 1). Slug/heading-parse logic mirrors the existing `re` usage in `tools.py:54` (`re.findall(r"\w+", ...)`).

---

### `tests/test_retrieval.py` (NEW — test, unit)

**Analog:** `tests/test_tools.py:15-56` — the direct-executor-call style. Copy this shape for cosine/floor/hybrid/degrade/citation-id tests:

**Direct executor + JSON-parse assertion** (`tests/test_tools.py:15-23`):
```python
def test_search_docs_grounds_billing_questions(registry):
    result = json.loads(registry["search_docs"].execute(query="refund policy"))
    docs = [r["doc"] for r in result["results"]]
    ...

def test_search_docs_no_match(registry):
    result = json.loads(registry["search_docs"].execute(query="zzzzz qqqqq"))
    assert result["results"] == []
```
The `results == []` floor test (D-03) is a direct extension of `test_search_docs_no_match`. Use `monkeypatch.setattr(settings, "voyage_api_key", None)` to assert the keyword baseline (RESEARCH Wave 0 gap).

**Fixtures available** (`tests/conftest.py`): `registry` (L42, `build_registry(conn, KB_DIR)`), `conn` (L24). Real `kb/*.md` is used via `KB_DIR`.

---

### `tests/test_index.py` (NEW — test, unit)

**Analog:** RESEARCH Code Example 5. Imports the SAME `kb_sha256` the builder stamped with. Reads `kb/index.json` directly (no fixture needed) and asserts `meta["kb_sha256"] == kb_sha256()`. Runs inside the existing `pytest -q` CI step — zero Voyage calls, no workflow edit (RESEARCH Pattern 1).

---

### `src/relay/tools.py` (MOD — search_docs swap + send_reply citations)

**Analog:** itself. **Preserve the output envelope** — `search_docs` today returns `json.dumps({"results": [...]})` (`tools.py:64`); the swap keeps `{"results": [...]}` and ADDS `retrieval_mode`/`degraded` keys (additive). Each result gains `{doc, heading, id, text, score}` (D-06) but keeps full-doc text (D-01).

**Current search_docs to replace** (`tools.py:48-64`) — note the docstring already anticipates this swap ("the embeddings-based retriever replaces the scoring here without changing the tool contract"):
```python
def search_docs(kb_dir: Path, query: str, max_results: int = 3) -> str:
    ...
    scored.sort(reverse=True)
    results = [{"doc": name, "content": text} for _, name, text in scored[:max_results]]
    return json.dumps({"results": results})
```
Keep it a **sync** function (`Callable[..., str]`, `ToolSpec.execute` contract at `tools.py:31`). It runs inside the Phase-2 `to_thread` seam (see agent assignment). Do NOT make it `async`.

**send_reply — add optional citations** (`tools.py:79-87`). Give the param a default so all existing scripted calls stay green (D-12/Pitfall 2):
```python
def send_reply(db: Database, ticket_id: int, body: str) -> str:
    with db.transaction():
        cur = db.execute(
            "INSERT INTO replies (ticket_id, body) VALUES (?, ?)", (ticket_id, body)
        )
        db.execute("UPDATE tickets SET status = 'resolved' WHERE id = ?", (ticket_id,))
        reply_id = cur.lastrowid
    return json.dumps({"reply_id": reply_id, "status": "resolved"})
```
→ signature becomes `send_reply(db, ticket_id, body, citations=())`. The `transaction()` context manager is safe to use here (Phase 2 WR-01 nest-safety CLOSED per CONTEXT canonical refs).

**build_registry closures** (`tools.py:96-219`) — the `search_docs` closure (`execute=lambda query: search_docs(kb_dir, query)`, L140) captures the loaded index; the `send_reply` closure (L191, `execute=lambda ticket_id, body: send_reply(conn, ticket_id, body)`) grows a `citations` param. Both tool **schemas** gain new `input_schema.properties` entries (`send_reply` schema L178-187 gains an optional `citations` array; not added to `required`).

---

### `src/relay/agent.py` (MOD — thread retrieved_ids + citation guard)

**Analog:** itself. This is the highest-leverage reuse in the phase — the citation guard is a near-verbatim copy of the SEC-04 ticket-binding path.

**1. Thread `retrieved_ids` the way ticket_id is threaded.** `bind_to_ticket` (`agent.py:52-72`) is the constructor-injection template. Add `retrieved_ids: set[str] | None = None` as a **constructor** arg (captured by the closure), NOT an executor param — this keeps `execute`'s signature `(spec, name, raw_input, policy)` so `test_the_agent_loop_takes_no_binding_argument_to_forget` stays green:
```python
def bind_to_ticket(ticket_id: int):
    ...
    def execute(
        spec: ToolSpec | None, name: str, raw_input: dict[str, Any], policy: ToolPolicy
    ) -> tuple[str, bool]:
        return _execute_guarded(spec, name, raw_input, policy, bound_ticket_id=ticket_id)
    return execute
```
In `run_ticket`, build the per-run set beside the binding (`agent.py:160`) — never on the shared registry:
```python
    execute_bound = bind_to_ticket(ticket["id"])   # ← add retrieved_ids here, captured by reference
```

**2. Citation guard in `_execute_guarded`** — copy the ticket_binding branch (`agent.py:108-125`) structure exactly, placing the citation check AFTER it, before `spec.execute`. Add `retrieved_ids: set[str] | None = None` as a keyword-only param (mirrors `bound_ticket_id: int | object = UNBOUND` at L81). The recoverable-denial wording + `denied_by` discriminator + `(str, True)` return is the exact template (arity MUST stay `(str, bool)` — `mcp_server.py:120` unpacks two):
```python
        return json.dumps({
            "error": (
                f"ticket_id {supplied_ticket_id} is not this run's ticket."
                f" This run may only act on ticket {bound_ticket_id}."
                f" Retry with ticket_id={bound_ticket_id}."
            ),
            "denied_by": "ticket_binding",
            "expected_ticket_id": bound_ticket_id,
            "supplied_ticket_id": supplied_ticket_id,
        }), True
```
→ citation version uses `denied_by="citation"`, `missing_citations`, `retrieved_ids` (RESEARCH Code Example 4). The MCP path passes no `retrieved_ids` (default `None`) → check skipped, exactly as `bound_ticket_id=UNBOUND` skips binding.

**3. Accumulate ids after the offloaded call returns** — the loop already parses `payload = json.loads(result)` (`agent.py:249`); add id-accumulation there (single-threaded, on the event loop, no race):
```python
                        result, is_error = await asyncio.to_thread(
                            execute_bound, spec, block.name, block.input, policy
                        )
                        payload = json.loads(result)
```

**4. Guardrail event branch** — copy the `binding_violation` branch (`agent.py:250-293`) for the citation guard: detect `payload.get("denied_by") == "citation"`, `logger.warning("guardrail.citation_unretrieved", ...)`, and yield a `guardrail` AgentEvent BEFORE the `tool_result` (cause-then-effect):
```python
                        binding_violation = (
                            is_error and payload.get("denied_by") == "ticket_binding"
                        )
                    ...
                    if binding_violation:
                        logger.warning("guardrail.ticket_id_mismatch", ...)
                        # Cause before effect: the stream shows the denial, then its result.
                        yield AgentEvent(
                            type="guardrail",
                            data={"guard": "ticket_binding", "tool": block.name, ...,
                                  "action": "denied"},
                        )
```

**5. Degradation event (RAG-05)** — on a `search_docs` tool_result with `degraded: true` in the payload, emit an additive event. D-14 locks a **distinct `type="notice"`** (not overloaded onto `guardrail`), `data={"kind": "retrieval_degraded", ...}`, plus `logger.warning("retrieval.degraded", ...)`. SSE framing is type-agnostic (`main.py:234` — see Shared Patterns), so a new type needs zero serialization changes.

**Off-loop seam already exists** (`agent.py:246-248`) — proves `search_docs` can stay sync with a blocking `httpx.post`:
```python
                        result, is_error = await asyncio.to_thread(
                            execute_bound, spec, block.name, block.input, policy
                        )
```

---

### `src/relay/guardrails.py` (MOD — SendReplyInput gains optional citations)

**Analog:** itself. `SendReplyInput` (`guardrails.py:28-30`) gains `citations: list[str] = Field(default_factory=list)` (D-12 optional — `[] ⊆ retrieved` always passes, keeps ~7 test files green). `SearchDocsInput` (L19-20) is unchanged in shape (query already `max_length=500`; note the ASVS input-validation cap). Field-constraint style to copy:
```python
class SendReplyInput(BaseModel):
    ticket_id: int = Field(gt=0)
    body: str = Field(min_length=20, max_length=10_000)
```

---

### `src/relay/config.py` (MOD — Voyage settings)

**Analog:** the `anthropic_api_key` escape hatch (`config.py:21-22`) — the EXACT pattern for `voyage_api_key` (Pitfall 4: `RELAY_` prefix would silently rename the key):
```python
    # Read without the RELAY_ prefix so the same variable works for the SDK's own lookup.
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
```
→ `voyage_api_key: str | None = Field(default=None, validation_alias="VOYAGE_API_KEY")`. Also add `voyage_model: str = "voyage-4-lite"`, `voyage_dim: int = 512`, `retrieval_floor: float = <calibrated>` (D-04 output) as plain typed defaults alongside the existing phase-grouped settings (e.g. the guardrails block L62-65).

---

### `src/relay/models.py` (MOD — AgentEvent type comment)

**Analog:** itself. `AgentEvent.type` is a free `str` (`models.py:42`); only the doc comment enumerating types (L41) needs the new `notice` (and the `guardrail` guard already covers citation). No structural change:
```python
    # "text" | "tool_use" | "tool_result" | "guardrail" | "usage" | "resolution" | "error"
    type: str
```

---

### `src/relay/prompts.py` (MOD — instruct citing)

**Analog:** itself. `SYSTEM_PROMPT` (`prompts.py:1-26`) already instructs grounding in step 3 ("Every factual claim ... must be grounded in a returned doc"). Extend the `send_reply` bullet (L12-13) to instruct citing the `id`s returned by `search_docs`. Keep the terse imperative voice.

---

### `pyproject.toml` (MOD — deps)

**Analog:** the `dependencies` list (`pyproject.toml:9-19`). Add `"numpy>=2.3,<3"` (NOT `>=2.5` — 3.11 floor) and promote `"httpx>=0.28,<1"` to runtime (currently only in `[dev]` extra at L25, transitive via `anthropic`). Follow the existing bounded-version + trailing-comma style.

---

### `.env.example` (MOD)

**Analog:** itself. Add `VOYAGE_API_KEY=` (un-prefixed, matching the `validation_alias`) next to the existing `ANTHROPIC_API_KEY=sk-ant-...` line.

---

## Shared Patterns

### Recoverable model-facing denial (SEC-04 → citation)
**Source:** `src/relay/agent.py:108-125` (ticket_binding branch)
**Apply to:** the new citation guard in `_execute_guarded`
The denial is phrased as a **retry instruction**, not a refusal (avoids Pitfall 1 `ended_without_action`), returns `(json, True)`, carries a `denied_by` discriminator, and never raises:
```python
        return json.dumps({
            "error": ("ticket_id ... is not this run's ticket. ... Retry with ...=..."),
            "denied_by": "ticket_binding",
            "expected_ticket_id": bound_ticket_id,
            "supplied_ticket_id": supplied_ticket_id,
        }), True
```

### Constructor-injected per-run constraint (never on the shared registry)
**Source:** `src/relay/agent.py:52-72` (`bind_to_ticket`) + L160 usage
**Apply to:** threading `retrieved_ids` into the executor. The registry is built once and shared by every concurrent run; the per-run set is captured by the per-run `bind_to_ticket` closure (avoids cross-run leak, milestone Pitfall 2).

### `guardrail` event ordered before its `tool_result`
**Source:** `src/relay/agent.py:250-293`
**Apply to:** citation-guard event and the degradation `notice` event. Cause-then-effect ordering; `logger.warning("guardrail.<name>", extra={"ctx": {...}})` with a dotted event name.

### Type-agnostic SSE framing (new event types are free)
**Source:** `src/relay/main.py:234`
**Apply to:** `notice` (degradation) and `guardrail` (citation) events — no serialization change needed:
```python
                yield f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"
```

### `validation_alias` env escape hatch
**Source:** `src/relay/config.py:22`
**Apply to:** `voyage_api_key` (Pitfall 4).

### Sync executor + `to_thread` off-loop seam
**Source:** `src/relay/agent.py:246-248`; contract at `src/relay/tools.py:31` (`Callable[..., str]`); asserted by `tests/test_lifecycle.py:264-292`
**Apply to:** `search_docs` (blocking `httpx.post` stays sync — do NOT make it async; would break MCP sync path `mcp_server.py:120` and the off-loop test).

### Binding-test template for the citation guard
**Source:** `tests/test_guardrails.py:220-309`
**Apply to:** `tests/test_guardrails.py` new citation tests. Direct 1:1 mirror:
- `test_mismatched_ticket_id_is_denied` (L220) → cited-id-not-retrieved is denied (`denied_by == "citation"`)
- `test_binding_denial_emits_guardrail_event` (L235) → guardrail event with `guard="citation"`, ordered before tool_result
- `test_run_recovers_after_binding_denial` (L255) → model retries with a valid citation, run reaches `resolution` (guards Pitfall 1)
- `test_the_agent_loop_takes_no_binding_argument_to_forget` (L289) → **must still pass**: `retrieved_ids` is a constructor arg, executor signature stays `[spec, name, raw_input, policy]`

### send_reply back-compat surface (do not break these)
**Source (calls without citations):** `tests/helpers.py:49-52`, `tests/test_guardrails.py` (L75, 106, 223, 238, 258, 273, 305, 337), `tests/test_lifecycle.py`, `tests/test_observability.py`, `tests/test_mcp.py`, `tests/test_tools.py:49`, `tests/test_db.py`
**Apply to:** D-12 — make `citations` optional (`default_factory=list`) + defaulted executor param so every citation-less scripted call passes (`[] ⊆ retrieved`).

---

## No Analog Found

| File | Role | Data Flow | Reason / Fallback |
|------|------|-----------|-------------------|
| `src/relay/retrieval.py` (Voyage `httpx.post` body only) | service | request-response | No existing outbound-HTTP client — all HTTP is via the Anthropic SDK. Module *shape* copies `tools.py`; the POST follows RESEARCH Code Example 2 (sync `httpx`, timeout + one retry, degrade-never-raise). numpy cosine follows RESEARCH "Don't Hand-Roll" (`mat @ q_norm`, `np.argsort`). |
| `scripts/build_index.py` | script | batch/file-I/O | `scripts/` holds only `demo.sh` (bash) — no Python-script analog. Follow RESEARCH Code Example 1 verbatim; reuse `retrieval.kb_sha256`/`headings` helpers. |
| `kb/index.json` | artifact | file-I/O | New committed artifact; shape per RESEARCH Pattern 1 `meta`+`docs`. |

---

## Metadata

**Analog search scope:** `src/relay/` (all modules), `tests/`, `scripts/`, `pyproject.toml`, `Dockerfile`, `.env.example`
**Files scanned (read in full or targeted):** agent.py, tools.py, guardrails.py, config.py, models.py, prompts.py, test_guardrails.py, helpers.py, test_lifecycle.py (off-loop region), test_tools.py (grep), conftest.py (grep), main.py (grep), mcp_server.py (grep), pyproject.toml, Dockerfile, .env.example
**Pattern extraction date:** 2026-08-10
