"""Shared types and helpers for the rule modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from finops.config import Rules
from finops.providers.base import MetricSeries, Resource
from finops.savings.pricing import PriceBook

IDLE = "idle"
OVER = "over_provisioned"
RIGHT = "right_sized"
INSUFFICIENT = "insufficient_data"
CONFIG = "config"

CLASSIFICATIONS = [IDLE, OVER, CONFIG, RIGHT, INSUFFICIENT]


@dataclass
class Recommendation:
    resource_id: str
    resource_type: str
    classification: str
    confidence: float = 0.0
    risk_tier: str = "manual_only"
    current_spec: dict[str, Any] = field(default_factory=dict)
    recommended_spec: dict[str, Any] = field(default_factory=dict)
    action_type: str | None = None
    current_monthly_cost: float = 0.0
    recommended_monthly_cost: float = 0.0
    rationale: dict[str, Any] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def monthly_savings(self) -> float:
        if self.classification in (RIGHT, INSUFFICIENT):
            return 0.0
        return round(max(0.0, self.current_monthly_cost - self.recommended_monthly_cost), 4)


@dataclass
class Context:
    rules: Rules
    prices: PriceBook
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


class MetricView:
    """Window-limited read access to a resource's hourly series."""

    def __init__(self, series: dict[str, MetricSeries], now: datetime, window_days: float):
        start = now - timedelta(days=window_days)
        self.window_days = window_days
        self.series = {
            name: [p for p in s.points if _aware(p.ts) >= start and _aware(p.ts) <= now] for name, s in series.items()
        }
        self.periods = {name: s.period_seconds for name, s in series.items()}

    def has(self, metric: str) -> bool:
        return bool(self.series.get(metric))

    def days(self, metric: str) -> float:
        pts = self.series.get(metric) or []
        if not pts:
            return 0.0
        span = (_aware(pts[-1].ts) - _aware(pts[0].ts)).total_seconds() / 86400
        return round(span + 1 / 24, 3)

    def coverage(self, metric: str) -> float:
        """Fraction of expected hourly points present over the observed span."""
        pts = self.series.get(metric) or []
        days = self.days(metric)
        if not pts or days <= 0:
            return 0.0
        return min(1.0, len(pts) / (days * 24))

    def values(self, metric: str, field_name: str = "p95") -> np.ndarray:
        return np.array([getattr(p, field_name) for p in self.series.get(metric) or []], dtype=float)

    def p95(self, metric: str) -> float | None:
        v = self.values(metric, "p95")
        return float(np.percentile(v, 95)) if v.size else None

    def peak(self, metric: str) -> float | None:
        v = self.values(metric, "max")
        return float(v.max()) if v.size else None

    def mean(self, metric: str) -> float | None:
        v = self.values(metric, "p50")
        return float(v.mean()) if v.size else None

    def low(self, metric: str, pct: float = 5) -> float | None:
        v = self.values(metric, "p50")
        return float(np.percentile(v, pct)) if v.size else None

    def total(self, metric: str) -> float:
        return float(self.values(metric, "sum").sum())

    def period(self, metric: str) -> int:
        return self.periods.get(metric, 300)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    return _aware(datetime.fromisoformat(value))


def r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def base_rec(resource: Resource, classification: str, **kw: Any) -> Recommendation:
    return Recommendation(
        resource_id=resource.id,
        resource_type=resource.type,
        classification=classification,
        current_spec=dict(resource.spec),
        tags=dict(resource.tags),
        **kw,
    )
