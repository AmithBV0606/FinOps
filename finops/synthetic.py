"""Synthetic utilisation series for unit tests, the offline demo and sensitivity analysis.

Series are generated at 5-minute resolution and pushed through the same hourly aggregation the
collector uses, so the analyzer sees exactly the shape it would see from CloudWatch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from finops.providers.aggregate import aggregate_hourly
from finops.providers.base import MetricSeries


def series(
    resource_id: str,
    metric: str,
    days: float = 30,
    base: float = 10.0,
    noise: float = 1.0,
    diurnal: float = 0.0,
    spike_every_hours: float | None = None,
    spike_height: float = 0.0,
    clip: tuple[float, float] | None = (0.0, 100.0),
    end: datetime | None = None,
    period: int = 300,
    seed: int = 0,
    gap_fraction: float = 0.0,
) -> MetricSeries:
    """``base`` ± ``noise`` with an optional business-hours bump (``diurnal``) and periodic spikes."""
    rng = np.random.default_rng(seed)
    end = (end or datetime.now(UTC)).replace(second=0, microsecond=0)
    n = int(days * 86400 / period)
    ts = [end - timedelta(seconds=period * (n - i)) for i in range(n)]
    hours = np.array([t.hour + t.minute / 60 for t in ts])
    values = base + rng.normal(0, noise, n)
    if diurnal:
        values += diurnal * np.clip(np.sin((hours - 6) / 12 * np.pi), 0, None)
    if spike_every_hours:
        step = int(spike_every_hours * 3600 / period)
        values[::step] += spike_height
    if clip is not None:
        values = np.clip(values, *clip)
    keep = rng.random(n) >= gap_fraction
    points = aggregate_hourly((t, float(v)) for t, v, k in zip(ts, values, keep, strict=True) if k)
    return MetricSeries(resource_id, metric, points, period)


def constant(resource_id: str, metric: str, value: float, days: float = 30, end: datetime | None = None,
             period: int = 300) -> MetricSeries:  # fmt: skip
    return series(resource_id, metric, days, base=value, noise=0.0, clip=None, end=end, period=period)
