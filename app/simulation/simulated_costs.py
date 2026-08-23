"""Cost aggregation for hypothetical simulations.

Thin composition layer over the phase-9 cost engine: fee rates, VWAP
slippage and funding all come from `app.costs`, never from a second
implementation.  This module only sums what the modelled legs produced
and expresses it relative to the technical risk.
"""

from __future__ import annotations

from app.simulation.models import SimulationCostBreakdown, SimulationPlanReference


def cost_to_risk_pct(
    costs: SimulationCostBreakdown, *, plan: SimulationPlanReference, quantity: float
) -> float | None:
    """Modelled frictions as a share of the technical risk distance."""
    reference_risk = plan.risk_per_unit * quantity
    if reference_risk <= 0:
        return None
    return costs.total / reference_risk * 100.0


def cost_to_gross_pct(costs: SimulationCostBreakdown, gross: float) -> float | None:
    """Modelled frictions as a share of the modelled gross result."""
    if gross == 0:
        return None
    return costs.total / abs(gross) * 100.0


def summarize_costs(costs: SimulationCostBreakdown) -> dict[str, float]:
    """Flat, user-safe summary of all modelled frictions."""
    return {
        "modelled_fees_total": round(costs.fees_total, 6),
        "modelled_slippage_total": round(costs.slippage_total, 6),
        "modelled_funding_total": round(costs.funding_cost, 6),
        "modelled_costs_total": round(costs.total, 6),
    }
