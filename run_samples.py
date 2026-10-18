#!/usr/bin/env python3
"""Run the pipeline over a fixed set of real filings, one per announcement type.

These are the cases behind REPORT.md. Each case is a real, public CNINFO filing.

    python run_samples.py                # full pipeline (needs ANTHROPIC_API_KEY)
    python run_samples.py --no-llm       # fetch + parse only
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import main as cli

#: (label, CLI args) - narrow date ranges so each case resolves to one filing.
CASES: list[tuple[str, list[str]]] = [
    ("dividend", ["--codes", "000001", "--category", "dividend",
                  "--start", "2025-09-01", "--end", "2025-10-01", "--limit", "1"]),
    ("related_party_transaction", ["--codes", "000002", "--keyword", "关联交易",
                                   "--start", "2025-11-10", "--end", "2025-11-15", "--limit", "1"]),
    ("acquisition", ["--codes", "002932", "--keyword", "收购",
                     "--start", "2025-12-25", "--end", "2025-12-31", "--limit", "1"]),
    ("earnings_report", ["--codes", "000002", "--category", "earnings_forecast",
                         "--start", "2025-07-01", "--end", "2025-07-31", "--limit", "1"]),
    ("share_pledge", ["--codes", "300691", "--keyword", "股份质押",
                      "--start", "2025-12-28", "--end", "2025-12-31", "--limit", "1"]),
    # Long filing: exercises the truncation path in parse.build_prompt_text.
    ("annual_report_long", ["--codes", "000002", "--category", "annual_report",
                            "--start", "2025-04-01", "--end", "2025-04-02", "--limit", "1"]),
]


def rebuild_summary(out_dir: Path) -> dict:
    """Aggregate every per-filing JSON in ``out_dir`` into ``summary.json``.

    Needed because each case invocation writes its own summary; this one covers
    the whole sample set.
    """
    records = [
        json.loads(path.read_text("utf-8"))
        for path in sorted(out_dir.glob("*.json"))
        if path.name != "summary.json"
    ]
    summary = cli.build_summary(records)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), "utf-8"
    )
    return summary


def run(extra: list[str]) -> int:
    failures = 0
    for label, args in CASES:
        print(f"\n{'=' * 72}\n== {label}\n{'=' * 72}")
        code = cli.main(args + extra)
        if code != 0:
            print(f"!! {label} exited {code}")
            failures += 1

    out_dir = Path("samples")
    if out_dir.exists():
        summary = rebuild_summary(out_dir)
        print(
            f"\nsample set: {summary['ok']}/{summary['count']} extracted, "
            f"{summary['total_input_tokens']} in / {summary['total_output_tokens']} out tokens, "
            f"${summary['total_cost_usd']:.5f}"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
