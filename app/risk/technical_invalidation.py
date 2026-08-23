"""Deterministic technical invalidation from a phase-8 candidate.

The invalidation is the structural price at which the research thesis is no
longer valid: below the relevant confirmed structure/reclaim/sweep level for
bullish candidates, above it for bearish ones, with a configurable safety
buffer in bps plus an ATR component.  It is an internal research risk
definition - never a stop-loss recommendation to a user.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.config import RiskSettings
from app.risk.enums import InvalidationBasis, PlanRejectionCode
from app.risk.models import RiskEngineError, TechnicalInvalidation
from app.risk.rounding import round_price

if TYPE_CHECKING:
    from app.risk.models import RiskEvaluationContext

_BASIS_BY_CANDIDATE_TYPE = {
    "BULLISH_SWEEP_REVERSAL": InvalidationBasis.SWEPT_LEVEL,
    "BEARISH_SWEEP_REVERSAL": InvalidationBasis.SWEPT_LEVEL,
    "BULLISH_RECLAIM_CONTINUATION": InvalidationBasis.STRUCTURE_LEVEL,
    "BEARISH_REJECTION_CONTINUATION": InvalidationBasis.STRUCTURE_LEVEL,
    "RANGE_BREAKOUT_UP": InvalidationBasis.RANGE_BOUNDARY,
    "RANGE_BREAKOUT_DOWN": InvalidationBasis.RANGE_BOUNDARY,
}


def derive_invalidation(
    context: RiskEvaluationContext,
    entry_reference_price: float,
    settings: RiskSettings,
) -> TechnicalInvalidation:
    candidate = context.candidate
    if not candidate.referenced_levels:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_UNAVAILABLE,
            "candidate carries no referenced technical level",
        )
    level = candidate.referenced_levels[0]
    try:
        level_price = float(level["price"])
        level_timeframe = str(level.get("timeframe", "15m"))
    except (KeyError, TypeError, ValueError):
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_UNAVAILABLE,
            "candidate referenced level is malformed",
        ) from None
    if level_price <= 0:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_UNAVAILABLE, "non-positive reference level"
        )
    atr = context.atr_5m
    if atr is None or atr <= 0:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_UNAVAILABLE,
            "no ATR available for the invalidation buffer - refusing to guess",
        )

    basis = _BASIS_BY_CANDIDATE_TYPE.get(
        candidate.candidate_type, InvalidationBasis.STRUCTURE_LEVEL
    )
    buffer_bps_component = level_price * settings.invalidation_buffer_bps / 10_000
    buffer_atr_component = atr * settings.invalidation_buffer_atr_multiple
    buffer = buffer_bps_component + buffer_atr_component

    bullish = candidate.bullish
    raw = level_price - buffer if bullish else level_price + buffer
    invalidation_price = round_price(
        raw,
        price_decimals=context.instrument.price_decimals,
        tick_size=context.instrument.tick_size,
        mode="down" if bullish else "up",
    )
    if invalidation_price <= 0:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_UNAVAILABLE, "invalidation collapsed to <= 0"
        )
    wrong_side = (
        invalidation_price >= entry_reference_price
        if bullish
        else invalidation_price <= entry_reference_price
    )
    if wrong_side:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_WRONG_SIDE,
            f"invalidation {invalidation_price:.6g} is not on the adverse side of "
            f"the reference entry {entry_reference_price:.6g}",
        )

    confidence = 0.8 if basis is not InvalidationBasis.STRUCTURE_LEVEL else 0.7
    side_word = "below" if bullish else "above"
    return TechnicalInvalidation(
        invalidation_id=uuid.uuid4(),
        candidate_id=candidate.candidate_id,
        direction=candidate.direction,
        invalidation_price=invalidation_price,
        reference_level_price=level_price,
        reference_level_type=basis,
        buffer_bps=settings.invalidation_buffer_bps,
        buffer_atr_component=buffer_atr_component,
        structure_reason=(
            f"{basis.value} of {candidate.candidate_type}: thesis structurally "
            f"invalid {side_word} {level_price:.6g} (buffer "
            f"{settings.invalidation_buffer_bps:.1f}bps + "
            f"{settings.invalidation_buffer_atr_multiple:g}xATR)"
        ),
        source_timeframe=level_timeframe,
        source_ids=(str(candidate.candidate_id),),
        confidence=confidence,
        technical_conditions=(
            f"5m close {side_word} {invalidation_price:.6g}",
            *candidate.invalidation_conditions,
        ),
        computed_at=context.as_of,
    )
