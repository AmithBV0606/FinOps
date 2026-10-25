"""Realised savings (§9.4): monthly spend of ``finops:managed=true`` resources, before vs after actions.

Uses Cost Explorer filtered by the ``finops:managed`` cost-allocation tag (activate it in the Billing
console first; tags only apply to costs incurred after activation). One CE call per month requested
($0.01 each). Results are stored as ``billing_snapshots`` rows (scope ``tag:finops:managed=true``)
and shown on the dashboard's Validation page.

    python -m evaluation.realised_savings --months 3
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from finops.collector import providers_from_settings
from finops.db import BillingSnapshot, configure, session_scope
from finops.validation import realised_savings

SCOPE = "tag:finops:managed=true"


def month_starts(months: int, today: datetime) -> list[datetime]:
    starts = [today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)]
    for _ in range(months - 1):
        starts.insert(0, (starts[0] - timedelta(days=1)).replace(day=1))
    return starts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--months", type=int, default=3, help="complete months to fetch, plus month-to-date")
    ap.add_argument("--db")
    ap.add_argument("--profile")
    args = ap.parse_args()
    configure(args.db)
    provider = providers_from_settings(profile=args.profile)[0]
    today = datetime.now(UTC)
    starts = month_starts(args.months + 1, today)
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else today
        if end <= start:
            continue
        with session_scope() as s:
            existing = s.scalar(select(BillingSnapshot).where(
                BillingSnapshot.scope == SCOPE, BillingSnapshot.period_start == start))  # fmt: skip
            if existing is not None and i + 1 < len(starts):
                continue  # closed months never change; only month-to-date is refreshed
        total = provider.get_cost_for_tag(start, end, "finops:managed", "true")
        with session_scope() as s:
            row = s.scalar(select(BillingSnapshot).where(
                BillingSnapshot.scope == SCOPE, BillingSnapshot.period_start == start))  # fmt: skip
            if row is None:
                row = BillingSnapshot(scope=SCOPE, period_start=start)
                s.add(row)
            row.period_end, row.total, row.fetched_at = end, round(total, 2), today
        print(f"{start:%Y-%m}: ${total:,.2f}")
    print(json.dumps(realised_savings(), indent=2, default=str))


if __name__ == "__main__":
    main()
