"""Human-readable cost summaries (research wording, no guarantees)."""

from __future__ import annotations

from typing import Any

from app.costs.models import CostEstimate


def summarize_costs(estimate: CostEstimate) -> dict[str, Any]:
    """Compact, disclaimer-safe cost summary for dashboards and logs.

    Contains only model estimates - no order parameters, no promises.
    """
    primary = estimate.primary_target
    return {
        "model": f"{estimate.cost_model_name}@{estimate.cost_model_version}",
        "fee_schedule": f"{estimate.fee_schedule.version} ({estimate.fee_schedule.source})",
        "execution_assumptions": estimate.execution_assumptions.version,
        "entry_fee": round(estimate.entry.fee_cost, 6),
        "invalidation_fee": round(estimate.invalidation.fee_cost, 6),
        "entry_slippage": round(estimate.entry.slippage_cost, 6),
        "invalidation_slippage": round(estimate.invalidation.slippage_cost, 6),
        "expected_funding_cost": round(estimate.funding.expected_cost, 6),
        "funding_state": estimate.funding.state.value,
        "cost_buffer": round(estimate.cost_buffer, 6),
        "cost_to_risk_pct": round(estimate.cost_to_risk_pct, 2),
        "net_rr_primary": (
            round(primary.net_rr, 3) if primary and primary.net_rr is not None else None
        ),
        "note": "Hypothetische Modellwerte unter versionierten Annahmen - keine Garantie.",
    }
