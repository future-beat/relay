"""Phase 4: the retrieval metrics as a standalone, unpaid, ungated report (WR-08).

Exists so `VOYAGE_API_KEY` can be scoped to the metric block alone. Wiring the
secret into the `python -m relay.evals` step also flips all 12 golden runs from
keyword to semantic ranking, because the same key feeds every `search_docs` call —
so the `pass_rate < 0.8` gate would start judging a configuration it has never been
measured under, and a failure could not be attributed to any code change. This
module gives CI a second process with its own environment: the graded suite keeps
the retrieval mode its threshold was calibrated against, and semantic recall is
still measured beside it.

Nothing here gates. It writes a JSON artifact and exits 0 even when the metrics
raise — a report-only number must never be able to fail a job (D-03).

Usage:
    python -m relay.retrieval_report [--output eval_results]
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import settings
from .evals import print_retrieval_summary, safe_retrieval_metrics


def build_report() -> dict[str, Any]:
    return {
        "ran_at": datetime.now(UTC).isoformat(),
        "kb_dir": str(settings.kb_dir),
        "retrieval_metrics": safe_retrieval_metrics(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Report-only retrieval metrics (never gates)")
    parser.add_argument("--output", type=Path, default=Path("eval_results"))
    args = parser.parse_args()

    report = build_report()
    args.output.mkdir(exist_ok=True)
    out_path = args.output / f"retrieval-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(report, indent=2))
    print_retrieval_summary(report["retrieval_metrics"])
    print(f"report: {out_path}")


if __name__ == "__main__":
    main()
