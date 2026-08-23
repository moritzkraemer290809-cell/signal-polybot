"""Result arithmetic of one hypothetical simulated position.

All values are model quantities in virtual reference units.  Nothing here
is realised, credited or withdrawn; "gross"/"net" describe the modelled
difference between two modelled reference prices minus modelled costs.
"""

from __future__ import annotations

from app.simulation.models import (
    ModelledExecution,
    ModelledFunding,
    SimulationCostBreakdown,
    SimulationPlanReference,
)


def gross_result(
    *,
    plan: SimulationPlanReference,
    entry: ModelledExecution,
    exit_execution: ModelledExecution,
    partial: ModelledExecution | None = None,
) -> float:
    """Modelled gross difference in virtual reference units."""
    sign = 1.0 if plan.bullish else -1.0
    total = 0.0
    remaining = entry.quantity
    if partial is not None:
        total += sign * (partial.modelled_price - entry.modelled_price) * partial.quantity
        remaining = max(0.0, remaining - partial.quantity)
    total += sign * (exit_execution.modelled_price - entry.modelled_price) * remaining
    return total


def cost_breakdown(
    *,
    entry: ModelledExecution,
    exit_execution: ModelledExecution,
    partial: ModelledExecution | None,
    funding: ModelledFunding | None,
) -> SimulationCostBreakdown:
    partial_fee = partial.fee_cost if partial is not None else 0.0
    partial_slippage = partial.slippage_cost if partial is not None else 0.0
    return SimulationCostBreakdown(
        entry_fee=entry.fee_cost,
        exit_fee=exit_execution.fee_cost + partial_fee,
        entry_slippage=entry.slippage_cost,
        exit_slippage=exit_execution.slippage_cost + partial_slippage,
        funding_cost=funding.funding_cost if funding is not None else 0.0,
    )


def net_result(gross: float, costs: SimulationCostBreakdown) -> float:
    """Modelled net result: every modelled friction counted exactly once."""
    return gross - costs.total


def risk_units(value: float, *, plan: SimulationPlanReference, quantity: float) -> float | None:
    """Express a modelled result in R (technical risk units).

    R uses the plan's technical risk per unit - an internal research
    normalisation, never a real money amount.
    """
    reference_risk = plan.risk_per_unit * quantity
    if reference_risk <= 0:
        return None
    return value / reference_risk
