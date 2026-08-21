"""Explainable, deterministic setup scoring (0-100).

The score is a research/setup-quality score only.  It is NOT a probability,
NOT profitability, NOT a win rate, and it never overrides hard exclusion
rules or later cost/risk validation.  Weights are configuration (validated
to sum to 100) and enter the configuration hash.
"""

from __future__ import annotations

from app.config import StrategySettings
from app.strategy.models import ScoreBreakdown


def compute_score(
    fractions: dict[str, tuple[float, str]], settings: StrategySettings
) -> tuple[int, list[ScoreBreakdown]]:
    """fractions: component -> (0..1 achieved fraction, reason)."""
    weights = settings.score_weights_json
    breakdown: list[ScoreBreakdown] = []
    total = 0.0
    for component, max_points in weights.items():
        fraction, reason = fractions.get(component, (0.0, "component missing"))
        fraction = max(0.0, min(1.0, fraction))
        awarded = round(fraction * max_points, 2)
        total += awarded
        breakdown.append(
            ScoreBreakdown(
                component=component, max_points=max_points, awarded=awarded, reason=reason
            )
        )
    return round(total), breakdown
