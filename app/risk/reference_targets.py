"""Technical reference target levels.

Targets come exclusively from confirmed public technical levels (liquidity
pools, swings, range boundaries, equal-level clusters) on the profitable
side of the reference entry.  Targets are never invented to reach a desired
R:R and are never take-profit orders.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.config import RiskSettings
from app.risk.enums import PlanRejectionCode, TargetLevelType
from app.risk.models import ReferenceTarget, RiskEngineError, TechnicalLevel

if TYPE_CHECKING:
    from app.risk.models import RiskEvaluationContext

_MAX_TARGETS = 3

_TYPE_MAP = {
    "SWING_HIGH": TargetLevelType.SWING_LEVEL,
    "SWING_LOW": TargetLevelType.SWING_LEVEL,
    "RANGE_HIGH": TargetLevelType.RANGE_BOUNDARY,
    "RANGE_LOW": TargetLevelType.RANGE_BOUNDARY,
    "EQUAL_HIGHS": TargetLevelType.EQUAL_LEVEL_CLUSTER,
    "EQUAL_LOWS": TargetLevelType.EQUAL_LEVEL_CLUSTER,
}


def _target_type(level: TechnicalLevel) -> TargetLevelType:
    return _TYPE_MAP.get(level.kind, TargetLevelType.LIQUIDITY_POOL)


def derive_targets(
    context: RiskEvaluationContext,
    entry_reference_price: float,
    settings: RiskSettings,
) -> tuple[ReferenceTarget, ...]:
    if entry_reference_price <= 0:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_TARGET_UNAVAILABLE, "invalid entry reference"
        )
    bullish = context.candidate.bullish
    relevant = [
        level
        for level in context.levels
        if level.price > 0 and level.relevance >= settings.target_min_relevance_score
    ]
    if not relevant:
        raise RiskEngineError(
            PlanRejectionCode.REFERENCE_TARGET_UNAVAILABLE,
            "no sufficiently relevant technical levels available",
        )
    # profitable side only - a minimal gap of the min distance guards against
    # targets sitting inside the immediate spread/noise band
    min_gap = entry_reference_price * settings.min_distance_bps / 10_000
    if bullish:
        candidates = [level for level in relevant if level.price > entry_reference_price + min_gap]
    else:
        candidates = [level for level in relevant if level.price < entry_reference_price - min_gap]
    if not candidates:
        raise RiskEngineError(
            PlanRejectionCode.TARGET_WRONG_SIDE,
            "technical levels exist, but none on the profitable side of the reference entry",
        )
    candidates.sort(key=lambda level: level.price, reverse=not bullish)
    targets = []
    for level in candidates[:_MAX_TARGETS]:
        distance_bps = abs(level.price - entry_reference_price) / entry_reference_price * 10_000
        targets.append(
            ReferenceTarget(
                target_id=uuid.uuid4(),
                price=level.price,
                target_type=_target_type(level),
                source_timeframe=level.timeframe,
                source_id=level.source_id,
                relevance=level.relevance,
                direction_compatible=True,
                distance_bps=distance_bps,
                confidence=min(0.9, level.relevance),
            )
        )
    return tuple(targets)
