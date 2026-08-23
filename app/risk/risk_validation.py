"""Hard cost/expectancy gates - a score can never override these."""

from __future__ import annotations

from app.config import RiskSettings
from app.costs.models import CostEstimate
from app.risk.enums import PlanRejectionCode
from app.risk.models import RiskEngineError


def validate_cost_estimate(estimate: CostEstimate, settings: RiskSettings) -> None:
    """Order matters: specific causes first, then the aggregate checks."""
    if estimate.gross_invalidation_loss <= 0:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_WRONG_SIDE, "non-positive gross technical risk"
        )
    if estimate.net_invalidation_loss <= 0:
        raise RiskEngineError(
            PlanRejectionCode.EVALUATION_ERROR,
            "net invalidation loss is not plausibly positive",
        )
    funding_pct = estimate.funding.expected_cost / estimate.gross_invalidation_loss * 100.0
    if funding_pct > settings.max_cost_to_risk_pct:
        raise RiskEngineError(
            PlanRejectionCode.FUNDING_COST_EXCESSIVE,
            f"expected funding cost alone consumes {funding_pct:.1f}% of the "
            f"technical risk (max {settings.max_cost_to_risk_pct:.1f}%)",
        )
    primary = estimate.primary_target
    if primary is None:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_TARGET_UNAVAILABLE, "no target economics available"
        )
    if primary.net_target_pnl <= 0:
        raise RiskEngineError(
            PlanRejectionCode.NET_TARGET_NON_POSITIVE,
            f"net PnL of the primary reference target is non-positive "
            f"({primary.net_target_pnl:.2f} pUSD after costs)",
        )
    if primary.net_rr is None or primary.net_rr < settings.min_net_rr:
        raise RiskEngineError(
            PlanRejectionCode.NET_RR_BELOW_THRESHOLD,
            f"net R:R {primary.net_rr if primary.net_rr is not None else 'n/a'} "
            f"below the minimum {settings.min_net_rr:g} (primary target)",
        )
    if estimate.cost_to_risk_pct > settings.max_cost_to_risk_pct:
        raise RiskEngineError(
            PlanRejectionCode.COST_TO_RISK_EXCESSIVE,
            f"total costs consume {estimate.cost_to_risk_pct:.1f}% of the technical "
            f"risk (max {settings.max_cost_to_risk_pct:.1f}%)",
        )
