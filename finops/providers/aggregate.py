"""Aggregate raw CloudWatch datapoints (5-min) into hourly p50/p95/max/sum rows."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import numpy as np
import pandas as pd

from .base import MetricPoint


def aggregate_hourly(datapoints: Iterable[tuple[datetime, float]]) -> list[MetricPoint]:
    rows = list(datapoints)
    if not rows:
        return []
    df = pd.DataFrame(rows, columns=["ts", "value"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["hour"] = df["ts"].dt.floor("h")
    out: list[MetricPoint] = []
    for hour, group in df.groupby("hour", sort=True):
        values = group["value"].to_numpy(dtype=float)
        out.append(
            MetricPoint(
                ts=hour.to_pydatetime(),
                p50=float(np.percentile(values, 50)),
                p95=float(np.percentile(values, 95)),
                max=float(values.max()),
                sum=float(values.sum()),
            )
        )
    return out
