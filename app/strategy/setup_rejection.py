"""SetupRejection construction."""

from __future__ import annotations

import uuid

from app.strategy.enums import CandidateType, RejectionCode
from app.strategy.models import EvaluationContext, SetupRejection


def build_rejection(
    context: EvaluationContext,
    primary: RejectionCode,
    detail: str,
    *,
    codes: tuple[RejectionCode, ...] | None = None,
    candidate_type: CandidateType | None = None,
) -> SetupRejection:
    return SetupRejection(
        rejection_id=uuid.uuid4(),
        instrument_pk=context.instrument_pk,
        instrument_id=context.instrument_id,
        symbol=context.symbol,
        as_of=context.as_of,
        primary_code=primary,
        codes=codes or (primary,),
        detail=detail,
        strategy_name=context.strategy_name,
        strategy_version=context.strategy_version,
        config_hash=context.config_hash,
        candidate_type=candidate_type,
    )
