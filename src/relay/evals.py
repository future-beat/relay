"""Phase 3: evaluation harness.

Runs the agent against a golden ticket dataset and grades each run twice:

1. Deterministically — did it take the expected terminal action, pick an
   acceptable category, and finish without errors?
2. With an LLM judge — is every claim in the reply/escalation grounded in the
   knowledge base, with no invented policy?

Produces a JSON report artifact and exits non-zero below a pass-rate
threshold, so it can gate CI.

Usage:
    python -m relay.evals [--limit N] [--concurrency 4] [--threshold 0.8]
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from .agent import run_ticket
from .config import settings
from .db import connect, init_db
from .models import AgentEvent
from .retrieval import load_index
from .retrieval_eval import (
    load_labels,
    mrr_from_scores,
    observed_mode,
    recall_from_scores,
    score_rows,
)
from .tools import build_registry, lookup_customer

GOLDEN_PATH = Path("evals/golden.jsonl")

JUDGE_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "grounded": {
                "type": "boolean",
                "description": "True only if every factual claim about the product, plans,"
                " pricing, or policy is supported by the knowledge base.",
            },
            "invented_claims": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Each factual claim not supported by the knowledge base.",
            },
            "quality": {
                "type": "integer",
                "enum": [1, 2, 3, 4, 5],
                "description": "Overall quality for the customer/handover reader:"
                " completeness, tone, actionability.",
            },
            "reasoning": {"type": "string"},
        },
        "required": ["grounded", "invented_claims", "quality", "reasoning"],
        "additionalProperties": False,
    },
}


def load_golden(path: Path = GOLDEN_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def retrieval_metrics() -> dict[str, Any]:
    """recall@1/@3 + MRR over the labeled set — report-only, never a gate (D-03).

    `mode` is part of the payload because keyword and semantic recall are not
    comparable numbers: without VOYAGE_API_KEY this measures the keyword
    fallback, and an unlabeled figure would be read as semantic quality (D-10).

    It is the mode the rows were ACTUALLY ranked in, read back out of `retrieve()`,
    not `bool(key)`. `retrieve()` never raises — it degrades — so a configured key
    is not evidence that semantic ranking ran: a missing/stale/mismatched
    `kb/index.json` or a failing Voyage call returns keyword results under a live
    secret, and that is the failure mode that reaches CI. `key_configured` and
    `degraded_rows` carry the gap between what was paid for and what ran.
    """
    index = load_index(settings.kb_dir)
    labels = load_labels()
    key = settings.voyage_api_key
    # One scoring pass: all three numbers and the mode label then describe the same
    # retrieval, instead of a label borrowed from a pass the numbers didn't come from.
    scores = score_rows(index, labels, k=3, key=key)
    return {
        "mode": observed_mode(scores),
        "key_configured": bool(key),
        "degraded_rows": sum(s.degraded for s in scores),
        "labeled_queries": len(labels),
        "scored_queries": len(scores),
        # recall@1 is `rank == 1` at depth 3: `retrieve(max_results=k)` truncates one
        # ranked list, so the top-3 prefix contains the top-1 answer exactly.
        "recall@1": recall_from_scores(scores, 1),
        "recall@3": recall_from_scores(scores, 3),
        "mrr": mrr_from_scores(scores),
    }


@dataclass
class CaseResult:
    id: str
    passed: bool
    action: str | None = None
    action_ok: bool = False
    category: str | None = None
    category_ok: bool = False
    grounded: bool | None = None
    invented_claims: list[str] = field(default_factory=list)
    quality: int | None = None
    cost_usd: float = 0.0
    error: str | None = None
    judge_reasoning: str | None = None
    # Observed, never graded (WR-10). `citations` is None when the model never passed
    # the argument at all, which is a different fact from `[]` and the one that decides
    # whether the citation guard is load-bearing or decorative.
    citations: list[str] | None = None
    retrieval: dict[str, Any] = field(default_factory=dict)


def extract_outcome(events: list[AgentEvent]) -> dict[str, Any]:
    """Pull the graded facts out of an agent run's event stream.

    `citations` and `retrieval` are recorded but not graded. Without them a report
    saying zero citation denials fired is unfalsifiable: it reads identically whether
    the model cited three retrieved ids and the guard correctly stayed quiet, or the
    model never cited anything and the guard could not have fired if it wanted to.
    """
    outcome: dict[str, Any] = {
        "action": None, "category": None, "final_text": None, "cost_usd": 0.0, "error": None,
        "citations": None,
        "retrieval": {"mode": None, "degraded": False, "retrieved_ids": []},
    }
    retrieved: set[str] = set()
    for event in events:
        if event.type == "tool_use":
            tool, tool_input = event.data["tool"], event.data["input"]
            if tool == "set_category":
                outcome["category"] = tool_input.get("category")
            elif tool == "send_reply":
                outcome["final_text"] = tool_input.get("body")
                # Overwritten alongside final_text, including on a denied attempt, so
                # the two always describe the same reply. Reporting the body of one
                # attempt beside the citations of another would be worse than silence.
                outcome["citations"] = tool_input.get("citations")
            elif tool == "create_escalation":
                outcome["final_text"] = tool_input.get("reason")
        elif event.type == "tool_result":
            if event.data["tool"] != "search_docs" or event.data["is_error"]:
                continue
            payload = event.data["result"]
            outcome["retrieval"]["mode"] = payload.get("retrieval_mode")
            # Sticky: one degraded search in a run is the fact worth keeping, and a
            # later healthy one must not erase it.
            outcome["retrieval"]["degraded"] |= bool(payload.get("degraded"))
            for hit in payload.get("results", []):
                # Mirrors the accept-set agent.py builds for the citation guard — doc
                # name, query-located id, and every anchor the whole file licenses.
                # Anything narrower would make a legitimately cited heading read as
                # unretrieved in the report and manufacture a violation that the
                # running system never saw.
                retrieved.update(x for x in (hit.get("doc"), hit.get("id")) if x)
                retrieved.update(a for a in hit.get("anchors") or () if a)
        elif event.type == "usage":
            outcome["cost_usd"] = event.data["cost_usd"]
        elif event.type == "resolution":
            outcome["action"] = event.data["via"]
        elif event.type == "error":
            outcome["error"] = event.data["reason"]
    outcome["retrieval"]["retrieved_ids"] = sorted(retrieved)
    return outcome


async def judge_grounding(
    client: AsyncAnthropic,
    case: dict[str, Any],
    final_text: str,
    kb_text: str,
    customer_record: str,
) -> dict[str, Any]:
    prompt = (
        "You are grading a support agent's output for factual grounding.\n\n"
        "<knowledge_base>\n"
        f"{kb_text}\n"
        "</knowledge_base>\n\n"
        "<customer_record>\n"
        "The agent had database access to this verified customer record and"
        " ticket history (statuses and timestamps included):\n"
        f"{customer_record}\n"
        "</customer_record>\n\n"
        "<customer_ticket>\n"
        f"Subject: {case['subject']}\n{case['body']}\n"
        "</customer_ticket>\n\n"
        "<agent_output>\n"
        f"{final_text}\n"
        "</agent_output>\n\n"
        "Check every factual claim the agent makes about the product, plans, pricing,"
        " limits, or policy against the knowledge base. Claims about the customer"
        " (name, plan, history) are grounded if consistent with the customer record."
        " Claims drawn from the ticket itself or clearly framed as unknowns/questions"
        " for a human are fine. Reasonable paraphrase is fine; invented specifics are not."
    )
    response = await client.messages.create(
        model=settings.model,
        max_tokens=2048,
        output_config={"format": JUDGE_SCHEMA},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


async def run_case(
    client: AsyncAnthropic,
    case: dict[str, Any],
    kb_text: str,
    *,
    seed_citation_denial: bool = False,
) -> CaseResult:
    """Run one golden case end to end and grade it.

    `seed_citation_denial` forwards the D-08 probe to the agent loop. It stays off for
    all 12 golden cases and for the `pass_rate` gate: the single armed case is a
    report-only, manually-dispatched finding about whether a real model recovers from
    a citation denial, not a threshold anything can fail on.
    """
    conn = connect(":memory:")
    init_db(conn)
    cur = conn.execute(
        "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
        (case["customer_email"], case["subject"], case["body"]),
    )
    conn.commit()
    ticket = {
        "id": cur.lastrowid,
        "customer_email": case["customer_email"],
        "subject": case["subject"],
        "body": case["body"],
    }
    registry = build_registry(conn, settings.kb_dir)

    events = [
        e async for e in run_ticket(
            client, registry, ticket, seed_citation_denial=seed_citation_denial
        )
    ]
    outcome = extract_outcome(events)
    # Give the judge exactly what the agent could see: the lookup_customer tool
    # output (profile + ticket history) and when the ticket was filed.
    filed_at = conn.execute(
        "SELECT created_at FROM tickets WHERE id = ?", (ticket["id"],)
    ).fetchone()[0]
    customer_record = (
        lookup_customer(conn, case["customer_email"])
        + f"\nThe ticket under review was filed at {filed_at} UTC."
    )
    conn.close()

    result = CaseResult(
        id=case["id"],
        passed=False,
        action=outcome["action"],
        category=outcome["category"],
        cost_usd=outcome["cost_usd"],
        error=outcome["error"],
        citations=outcome["citations"],
        retrieval=outcome["retrieval"],
    )
    result.action_ok = outcome["action"] == case["expected_action"]
    result.category_ok = outcome["category"] in case["expected_categories"]

    if outcome["final_text"]:
        verdict = await judge_grounding(
            client, case, outcome["final_text"], kb_text, customer_record
        )
        result.grounded = verdict["grounded"]
        result.invented_claims = verdict["invented_claims"]
        result.quality = verdict["quality"]
        result.judge_reasoning = verdict["reasoning"]

    result.passed = bool(result.action_ok and result.category_ok and result.grounded)
    return result


async def run_evals(limit: int | None, concurrency: int) -> dict[str, Any]:
    cases = load_golden()[:limit]
    kb_text = "\n\n---\n\n".join(
        f"## {p.name}\n{p.read_text()}" for p in sorted(settings.kb_dir.glob("*.md"))
    )
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(case: dict[str, Any]) -> CaseResult:
        async with semaphore:
            try:
                return await run_case(client, case, kb_text)
            except Exception as exc:  # noqa: BLE001 — one bad case must not sink the suite
                return CaseResult(id=case["id"], passed=False, error=f"harness: {exc}")

    results = await asyncio.gather(*(bounded(c) for c in cases))
    passed = sum(r.passed for r in results)
    qualities = [r.quality for r in results if r.quality is not None]
    return {
        "ran_at": datetime.now(UTC).isoformat(),
        "model": settings.model,
        "cases": len(results),
        "passed": passed,
        "pass_rate": round(passed / len(results), 3) if results else 0.0,
        "mean_quality": round(sum(qualities) / len(qualities), 2) if qualities else None,
        "total_cost_usd": round(sum(r.cost_usd for r in results), 4),
        # Report-only (D-03): printed and archived, never compared to a threshold.
        "retrieval_metrics": retrieval_metrics(),
        "results": [asdict(r) for r in results],
    }


def print_summary(report: dict[str, Any]) -> None:
    print(f"\n{'case':<24} {'action':<20} {'cat':<4} {'grounded':<9} {'q':<3} pass")
    print("-" * 68)
    for r in report["results"]:
        print(
            f"{r['id']:<24} "
            f"{(r['action'] or r['error'] or '—'):<20} "
            f"{'ok' if r['category_ok'] else 'X':<4} "
            f"{r['grounded']!s:<9} "
            f"{r['quality'] or '—'!s:<3} "
            f"{'PASS' if r['passed'] else 'FAIL'}"
        )
    print("-" * 68)
    print(
        f"pass rate {report['pass_rate']:.0%} ({report['passed']}/{report['cases']})"
        f" | mean quality {report['mean_quality']}"
        f" | agent cost ${report['total_cost_usd']}"
    )
    m = report.get("retrieval_metrics")
    if m:
        # recall@1 and MRR lead: recall@3 saturates on a 3-doc corpus (D-09).
        print(
            f"retrieval ({m['mode']}) recall@1 {m['recall@1']:.2f}"
            f" | MRR {m['mrr']:.2f}"
            f" | recall@3 {m['recall@3']:.2f}"
            f" over {m['scored_queries']} labeled queries — report-only, not gated"
        )
        if m.get("key_configured") and m["mode"] != "semantic":
            # Paid for semantic ranking, did not get it. Silent here is how keyword
            # numbers get read as semantic quality — the thing `mode` exists to stop.
            print(
                f"  WARNING: VOYAGE_API_KEY is set but {m['degraded_rows']} of"
                f" {m['scored_queries']} rows degraded — these are NOT semantic numbers."
                " Rebuild/commit kb/index.json or check Voyage."
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Relay eval suite")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--threshold", type=float, default=0.8)
    parser.add_argument("--output", type=Path, default=Path("eval_results"))
    args = parser.parse_args()

    report = asyncio.run(run_evals(args.limit, args.concurrency))
    args.output.mkdir(exist_ok=True)
    out_path = args.output / f"eval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print_summary(report)
    print(f"report: {out_path}")

    if report["pass_rate"] < args.threshold:
        print(f"FAIL: pass rate below threshold {args.threshold:.0%}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
