"""SignalRejection construction."""

from __future__ import annotations

import uuid
from datetime import datetime

from app.signals.enums import SignalRejectionCode
from app.signals.models import PlanSnapshot, SignalRejection


def build_admission_rejection(
    plan: PlanSnapshot,
    code: SignalRejectionCode,
    detail: str,
    *,
    lifecycle_model_version: str,
    lifecycle_config_hash: str,
    as_of: datetime,
) -> SignalRejection:
    return SignalRejection(
        rejection_id=uuid.uuid4(),
        plan_id=plan.plan_id,
        candidate_id=plan.candidate_id,
        signal_id=None,
        instrument_pk=plan.instrument_pk,
        symbol=plan.symbol,
        as_of=as_of,
        primary_code=code,
        codes=(code,),
        detail=detail,
        lifecycle_model_version=lifecycle_model_version,
        lifecycle_config_hash=lifecycle_config_hash,
    )


def build_lifecycle_rejection(
    *,
    signal_id: uuid.UUID,
    plan_id: uuid.UUID | None,
    candidate_id: uuid.UUID | None,
    instrument_pk: int,
    symbol: str,
    code: SignalRejectionCode,
    detail: str,
    lifecycle_model_version: str,
    lifecycle_config_hash: str,
    as_of: datetime,
) -> SignalRejection:
    """Structured record for invalid transitions / version conflicts."""
    return SignalRejection(
        rejection_id=uuid.uuid4(),
        plan_id=plan_id,
        candidate_id=candidate_id,
        signal_id=signal_id,
        instrument_pk=instrument_pk,
        symbol=symbol,
        as_of=as_of,
        primary_code=code,
        codes=(code,),
        detail=detail,
        lifecycle_model_version=lifecycle_model_version,
        lifecycle_config_hash=lifecycle_config_hash,
    )
