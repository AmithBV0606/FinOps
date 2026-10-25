"""Threshold sensitivity (§9.6): vary CPU/network thresholds and report flagged count and savings.

python -m evaluation.sensitivity                 # over the latest data in the DB
(also called by evaluation.azure_traces.run for the trace population)
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from finops.analyzer import Context, analyze
from finops.config import Rules, get_rules
from finops.providers.base import MetricSeries, Resource
from finops.savings.pricing import PriceBook

RESULTS = Path(__file__).parent / "results"

VARIANTS = [
    ("baseline", {}),
    ("over_cpu -10pp", {"ec2.over_provisioned.p95_cpu_below": -10, "rds.over_provisioned.p95_cpu_below": -10}),
    ("over_cpu +10pp", {"ec2.over_provisioned.p95_cpu_below": +10, "rds.over_provisioned.p95_cpu_below": +10}),
    ("idle_cpu -2.5pp", {"ec2.idle.p95_cpu_below": -2.5}),
    ("idle_cpu +10pp", {"ec2.idle.p95_cpu_below": +10}),
    ("idle_net x0.5", {"ec2.idle.network_mb_per_day_below": "x0.5"}),
    ("idle_net x10", {"ec2.idle.network_mb_per_day_below": "x10"}),
    ("headroom -10pp", {"ec2.headroom.max_projected_p95_cpu": -10}),
    ("headroom +10pp", {"ec2.headroom.max_projected_p95_cpu": +10}),
]


def variant_rules(base: Rules, changes: dict) -> Rules:
    rules = base.model_copy(deep=True)
    for path, delta in changes.items():
        *parents, leaf = path.split(".")
        obj = rules
        for p in parents:
            obj = getattr(obj, p)
        current = getattr(obj, leaf)
        new = current * float(delta[1:]) if isinstance(delta, str) else current + delta
        setattr(obj, leaf, max(0.0, new))
    return rules


def run(
    resources: list[Resource],
    series: dict[str, dict[str, MetricSeries]],
    now: datetime,
    base: Rules | None = None,
    variants: Iterable[tuple[str, dict]] = VARIANTS,
) -> list[dict]:
    base = base or get_rules()
    prices = PriceBook(provider=None, use_db=False)
    rows = []
    for name, changes in variants:
        recs = analyze(resources, series, Context(variant_rules(base, changes), prices, now))
        flagged = [r for r in recs if r.classification in ("idle", "over_provisioned", "config")]
        rows.append({
            "variant": name,
            "flagged": len(flagged),
            "idle": sum(r.classification == "idle" for r in recs),
            "over_provisioned": sum(r.classification == "over_provisioned" for r in recs),
            "monthly_savings_usd": round(sum(r.monthly_savings for r in recs), 2),
        })  # fmt: skip
    base_row = rows[0]
    for row in rows:
        row["flagged_delta"] = row["flagged"] - base_row["flagged"]
        row["savings_delta_pct"] = (
            round(100 * (row["monthly_savings_usd"] / base_row["monthly_savings_usd"] - 1), 1)
            if base_row["monthly_savings_usd"] else None
        )  # fmt: skip
    return rows


def print_table(rows: list[dict]) -> None:
    print(f"{'variant':18} {'flagged':>8} {'Δ':>5} {'idle':>6} {'over':>6} {'$/month':>12} {'Δ%':>7}")
    for r in rows:
        print(f"{r['variant']:18} {r['flagged']:>8} {r['flagged_delta']:>+5} {r['idle']:>6} "
              f"{r['over_provisioned']:>6} {r['monthly_savings_usd']:>12,.2f} "
              f"{'' if r['savings_delta_pct'] is None else format(r['savings_delta_pct'], '+.1f'):>7}")  # fmt: skip


def main() -> None:
    from finops.analyzer.engine import load_series, row_to_resource
    from finops.db import ResourceRow, configure, session_scope

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db")
    args = ap.parse_args()
    configure(args.db)
    now = datetime.now(UTC)
    with session_scope() as s:
        resources = [row_to_resource(r) for r in s.scalars(select(ResourceRow))]
        series = load_series(s, [r.id for r in resources], now - timedelta(days=30))
    rows = run(resources, series, now)
    print_table(rows)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "sensitivity_live.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
