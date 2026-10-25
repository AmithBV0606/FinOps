"""Confidence score (0–1) from window length, metric coverage, and variance (§4.4).

* window    = observed days / preferred days (capped at 1)
* coverage  = fraction of expected hourly points present
* stability = 1 / (1 + coefficient of variation) of the hourly p95 series — stable low usage
              scores high, spiky usage lower.
"""

from __future__ import annotations

import numpy as np

from finops.config import Rules


def stability(values: np.ndarray) -> float:
    if values.size < 2:
        return 0.0
    mean = float(values.mean())
    if mean <= 1e-9:
        return 1.0  # flat zero is perfectly stable
    cv = float(values.std()) / mean
    return 1.0 / (1.0 + cv)


def score(days: float, coverage: float, values: np.ndarray | None, rules: Rules) -> float:
    w = rules.confidence.weights
    window = min(1.0, days / rules.window.preferred_days) if rules.window.preferred_days else 1.0
    stab = stability(values) if values is not None else 1.0
    total = w.get("window", 0.4) * window + w.get("coverage", 0.3) * coverage + w.get("stability", 0.3) * stab
    norm = sum(w.values()) or 1.0
    return round(max(0.0, min(1.0, total / norm)), 3)
