"""Human-readable plan summaries.

Every rendering is explicitly research wording: model values, hypothetical
references, no guarantees, no trade instructions.  Reference price levels
appear ONLY in the local dashboard payload, clearly labelled as model
values.
"""

from __future__ import annotations

from typing import Any

from app.costs.explainability import summarize_costs
from app.risk.models import SignalEligibilityPlan

DISCLAIMER = (
    "Risk Research - kein Handelssignal. Keine reale Positions- oder "
    "Kontodatenbasis. Alle Preise sind hypothetische Modellwerte."
)


def summarize_plan(plan: SignalEligibilityPlan) -> dict[str, Any]:
    """Status-level summary WITHOUT reference price levels."""
    return {
        "plan_id": str(plan.plan_id),
        "status": plan.status.value,
        "candidate_type": f"{plan.candidate_type} ({plan.direction} research context)",
        "symbol": plan.symbol,
        "eligibility_score": plan.eligibility_score,
        "net_rr_primary": (
            round(plan.net_rr_primary, 3) if plan.net_rr_primary is not None else None
        ),
        "cost_to_risk_pct": round(plan.costs.cost_to_risk_pct, 2),
        "risk_model": f"{plan.risk_model_name}@{plan.risk_model_version}",
        "cost_model": f"{plan.cost_model_name}@{plan.cost_model_version}",
        "fee_schedule_version": f"{plan.fee_schedule_version} (assumption only)",
        "approximation_flags": list(plan.margin.approximation_flags),
        "warnings": list(plan.warnings),
        "as_of": plan.as_of.isoformat(),
        "expiry_at": plan.expiry_at.isoformat(),
        "note": "Interne Research-Eignungsbewertung - kein Trade-Signal.",
    }


def dashboard_plan_details(plan: SignalEligibilityPlan) -> dict[str, Any]:
    """Local dashboard payload - reference levels clearly marked as model
    values; never sent to Telegram and never an instruction."""
    return {
        **summarize_plan(plan),
        "score_components": [
            {
                "component": component.component,
                "max_points": component.max_points,
                "awarded": component.awarded,
                "reason": component.reason,
            }
            for component in plan.score_components
        ],
        "eligibility_reasons": list(plan.eligibility_reasons),
        "model_reference_levels": {
            "label": "Hypothetische Modellwerte - keine Handelsanweisung",
            "entry_zone": [plan.entry_zone.entry_low, plan.entry_zone.entry_high],
            "entry_reference": plan.entry_zone.entry_reference_price,
            "entry_basis": plan.entry_zone.entry_basis.value,
            "technical_invalidation": plan.invalidation.invalidation_price,
            "invalidation_basis": plan.invalidation.reference_level_type.value,
            "reference_targets": [
                {
                    "price": target.price,
                    "type": target.target_type.value,
                    "relevance": target.relevance,
                    "distance_bps": round(target.distance_bps, 1),
                }
                for target in plan.targets
            ],
        },
        "reference_position": {
            "label": "Virtuelle Referenzrechnung - keine echte Kontogroesse",
            "virtual_account_pusd": plan.position.virtual_account_pusd,
            "risk_per_plan_pct": plan.position.risk_per_plan_pct,
            "reference_cash_risk": round(plan.position.reference_cash_risk, 2),
            "reference_quantity": plan.position.reference_quantity,
            "reference_notional": round(plan.position.reference_notional, 2),
        },
        "leverage_suitability": {
            "label": "Konservativer Eignungsbereich - kein optimaler Hebel",
            "allowed_min": plan.leverage.allowed_leverage_min,
            "allowed_max": plan.leverage.allowed_leverage_max,
            "recommended_reference": plan.leverage.recommended_reference_leverage,
            "max_market_leverage": plan.leverage.max_market_leverage,
            "reasons": list(plan.leverage.suitability_reasons),
        },
        "margin_model": {
            "mode": plan.margin.mode.value,
            "approximated": plan.margin.approximated,
            "detail": plan.margin.detail,
        },
        "liquidation_buffer": plan.liquidation_buffer.detail,
        "costs": summarize_costs(plan.costs),
        "risk_distance": {
            "bps": round(plan.risk_distance.bps, 1),
            "atr_multiple": (
                round(plan.risk_distance.atr_multiple, 2)
                if plan.risk_distance.atr_multiple is not None
                else None
            ),
        },
        "versions": {
            "risk_config_hash": plan.risk_config_hash,
            "cost_config_hash": plan.cost_config_hash,
            "execution_assumptions": plan.execution_assumption_version,
            "instrument_snapshot": plan.instrument_snapshot_version,
            "strategy": f"{plan.strategy_name}@{plan.strategy_version}",
        },
        "data_timestamps": plan.data_timestamps,
        "disclaimer": DISCLAIMER,
    }
