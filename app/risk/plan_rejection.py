"""RiskPlanRejection construction."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.risk.enums import PlanRejectionCode
from app.risk.models import RiskPlanRejection

if TYPE_CHECKING:
    from app.risk.models import RiskEvaluationContext


def build_rejection(
    context: RiskEvaluationContext,
    primary: PlanRejectionCode,
    detail: str,
    *,
    codes: tuple[PlanRejectionCode, ...] | None = None,
) -> RiskPlanRejection:
    return RiskPlanRejection(
        rejection_id=uuid.uuid4(),
        candidate_id=context.candidate.candidate_id,
        instrument_pk=context.candidate.instrument_pk,
        instrument_id=context.candidate.instrument_id,
        symbol=context.candidate.symbol,
        as_of=context.as_of,
        primary_code=primary,
        codes=codes or (primary,),
        detail=detail,
        risk_model_name=context.risk_model_name,
        risk_model_version=context.risk_model_version,
        risk_config_hash=context.risk_config_hash,
        cost_model_version=context.cost_model_version,
        cost_config_hash=context.cost_config_hash,
        fee_schedule_version=context.fee_schedule.version if context.fee_schedule else None,
    )
