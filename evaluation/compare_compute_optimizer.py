"""Agreement with AWS Compute Optimizer (§9.2) for the latest analysis run.

    finops collect          # fetches Compute Optimizer findings alongside inventory
    finops analyze
    python -m evaluation.compare_compute_optimizer

Writes evaluation/results/compute_optimizer_{summary.json,rows.csv}.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from finops.db import configure
from finops.validation import compare_latest

RESULTS = Path(__file__).parent / "results"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db")
    ap.add_argument("--run-id", type=int)
    args = ap.parse_args()
    configure(args.db)
    report = compare_latest(args.run_id)
    rows = report.pop("rows")
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "compute_optimizer_summary.json").write_text(json.dumps(report, indent=2))
    pd.DataFrame(rows).to_csv(RESULTS / "compute_optimizer_rows.csv", index=False)
    print(json.dumps(report, indent=2))
    if not report["compared"]:
        print("\nNo overlap: is Compute Optimizer enrolled (≥12 h ago) and was `finops collect` run since?")


if __name__ == "__main__":
    main()
