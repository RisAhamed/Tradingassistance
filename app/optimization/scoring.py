"""Pure walk-forward scoring functions (Phase H).

Formulas (all deterministic, no I/O, no config access — weights are passed
in from ``optimization.scoring``):

    score_window(metrics, weights)
        = w.net_pnl      * (net_pnl      / 1000)
        + w.profit_factor * profit_factor
        - w.max_drawdown  * (max_drawdown / 1000)
        + w.expectancy    * (expectancy   / 100)

Monetary metrics are divided by 1000 (the system's typical per-trade
notional scale) so every term is O(1) and no single metric dominates by
units alone. Missing metric keys read as 0.0. Drawdown enters negatively:
larger drawdowns lower the score.

    score_candidate(window_scores)
        = {mean, std, min, max, stability_penalty, combined}

with population std, ``stability_penalty = 0`` when ``std == 0`` else
``std / (|mean| + 1e-9)``, and ``combined = mean - stability_penalty``.
"""
from __future__ import annotations

import math
import statistics


def score_window(metrics: dict, weights: dict) -> float:
    """Score one window's metrics with the configured weights."""
    net_pnl = float(metrics.get("net_pnl", 0.0) or 0.0)
    profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
    max_drawdown = float(metrics.get("max_drawdown", 0.0) or 0.0)
    expectancy = float(metrics.get("expectancy", 0.0) or 0.0)
    return (
        float(weights.get("net_pnl", 0.0)) * (net_pnl / 1000.0)
        + float(weights.get("profit_factor", 0.0)) * profit_factor
        - float(weights.get("max_drawdown", 0.0)) * (max_drawdown / 1000.0)
        + float(weights.get("expectancy", 0.0)) * (expectancy / 100.0)
    )


def score_candidate(window_scores: list[float]) -> dict:
    """Aggregate per-fold scores: mean, std, min, max + stability penalty."""
    scores = [float(s) for s in window_scores]
    if not scores:
        return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0,
                "stability_penalty": 0.0, "combined": 0.0}
    mean = statistics.fmean(scores)
    std = statistics.pstdev(scores)
    if std == 0.0:
        stability_penalty = 0.0
    else:
        stability_penalty = std / (abs(mean) + 1e-9)
    return {
        "mean": mean,
        "std": std,
        "min": min(scores),
        "max": max(scores),
        "stability_penalty": stability_penalty,
        "combined": mean - stability_penalty,
    }


def is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)
