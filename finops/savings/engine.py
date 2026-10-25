"""Monthly cost and savings arithmetic (ARCHITECTURE.md §4.5)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from finops.providers import catalog

from .pricing import PriceBook

HOURS_PER_MONTH = catalog.HOURS_PER_MONTH


def compute_monthly(hourly: float | None) -> float:
    return round((hourly or 0.0) * HOURS_PER_MONTH, 4)


def ebs_monthly(
    prices: PriceBook,
    region: str,
    volume_type: str,
    size_gb: float,
    iops: float | None = None,
    throughput: float | None = None,
) -> float:
    cost = size_gb * prices.ebs_component(volume_type, "storage", region)
    if volume_type == "gp3":
        cost += max(0.0, (iops or 0) - catalog.GP3_BASELINE_IOPS) * prices.ebs_component("gp3", "iops", region)
        cost += max(0.0, (throughput or 0) - catalog.GP3_BASELINE_THROUGHPUT) * prices.ebs_component(
            "gp3", "throughput", region
        )
    elif volume_type in ("io1", "io2"):
        cost += (iops or 0) * prices.ebs_component(volume_type, "iops", region)
    return round(cost, 4)


def gp2_equivalent_gp3(size_gb: float) -> tuple[int, int]:
    """gp3 IOPS/throughput that match a gp2 volume's baseline performance."""
    gp2_iops = min(max(100, 3 * size_gb), 16000)
    iops = int(max(catalog.GP3_BASELINE_IOPS, gp2_iops))
    # gp2 sustains 250 MiB/s only from 334 GiB; below that the gp3 baseline (125) matches or exceeds it.
    throughput = 250 if size_gb >= 334 else catalog.GP3_BASELINE_THROUGHPUT
    return iops, throughput


def to_inr(usd: float, rate: float) -> float:
    return round(usd * rate, 2)


def aggregate(recommendations: Iterable[Any]) -> dict[str, Any]:
    """Totals by service, risk tier, classification and team/env tag.

    Accepts ORM rows or ``Recommendation`` dataclasses (anything with the same attribute names).
    """
    total_current = total_savings = 0.0
    by_service: dict[str, float] = defaultdict(float)
    by_tier: dict[str, float] = defaultdict(float)
    by_class: dict[str, int] = defaultdict(int)
    by_env: dict[str, float] = defaultdict(float)
    by_team: dict[str, float] = defaultdict(float)
    for rec in recommendations:
        tags = getattr(rec, "tags", None)
        if tags is None and getattr(rec, "resource", None) is not None:
            tags = rec.resource.tags
        tags = tags or {}
        rtype = getattr(rec, "resource_type", None) or (rec.resource.type if rec.resource else "?")
        total_current += rec.current_monthly_cost
        total_savings += rec.monthly_savings
        by_class[rec.classification] += 1
        if rec.monthly_savings > 0:
            by_service[rtype] += rec.monthly_savings
            by_tier[rec.risk_tier] += rec.monthly_savings
            by_env[(tags.get("env") or tags.get("Environment") or "untagged").lower()] += rec.monthly_savings
            by_team[tags.get("team") or tags.get("Team") or "untagged"] += rec.monthly_savings
    return {
        "total_current_monthly": round(total_current, 2),
        "total_monthly_savings": round(total_savings, 2),
        "waste_pct": round(100 * total_savings / total_current, 1) if total_current else 0.0,
        "by_service": {k: round(v, 2) for k, v in by_service.items()},
        "by_risk_tier": {k: round(v, 2) for k, v in by_tier.items()},
        "by_classification": dict(by_class),
        "by_env": {k: round(v, 2) for k, v in by_env.items()},
        "by_team": {k: round(v, 2) for k, v in by_team.items()},
    }
