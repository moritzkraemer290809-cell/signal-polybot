"""Risk distance between reference entry and technical invalidation."""

from __future__ import annotations

from app.config import RiskSettings
from app.risk.enums import PlanRejectionCode
from app.risk.models import RiskDistance, RiskEngineError


def compute_risk_distance(
    entry_reference_price: float,
    invalidation_price: float,
    atr: float | None,
) -> RiskDistance:
    absolute = abs(entry_reference_price - invalidation_price)
    bps = absolute / entry_reference_price * 10_000 if entry_reference_price > 0 else 0.0
    atr_multiple = absolute / atr if atr is not None and atr > 0 else None
    return RiskDistance(absolute=absolute, bps=bps, atr_value=atr, atr_multiple=atr_multiple)


def validate_risk_distance(distance: RiskDistance, settings: RiskSettings) -> None:
    """Too close = inside normal ATR noise; too far = disproportionate risk."""
    if distance.absolute <= 0:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_WRONG_SIDE,
            "zero distance between entry and invalidation",
        )
    if distance.bps < settings.min_distance_bps:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_TOO_CLOSE,
            f"distance {distance.bps:.1f}bps < minimum {settings.min_distance_bps:.1f}bps",
        )
    if distance.bps > settings.max_distance_bps:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_TOO_FAR,
            f"distance {distance.bps:.1f}bps > maximum {settings.max_distance_bps:.1f}bps",
        )
    if distance.atr_multiple is None:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_UNAVAILABLE,
            "no ATR available to validate the risk distance",
        )
    if distance.atr_multiple < settings.min_distance_atr_multiple:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_TOO_CLOSE,
            f"distance {distance.atr_multiple:.2f}xATR sits inside normal noise "
            f"(min {settings.min_distance_atr_multiple:g}xATR)",
        )
    if distance.atr_multiple > settings.max_distance_atr_multiple:
        raise RiskEngineError(
            PlanRejectionCode.INVALIDATION_TOO_FAR,
            f"distance {distance.atr_multiple:.2f}xATR is disproportionate "
            f"(max {settings.max_distance_atr_multiple:g}xATR)",
        )
