"""Isolated-margin research model.

Without public initial/maintenance margin information the model refuses to
make any liquidation-price statement and blocks by default.  An
approximated formula is available ONLY behind an explicit opt-in setting and
every result of it is prominently flagged ``APPROXIMATED_MARGIN_MODEL``.
Cross margin is never modelled.  The mark price is the primary reference.
"""

from __future__ import annotations

from app.config import RiskSettings
from app.risk.enums import MarginModelMode, PlanRejectionCode
from app.risk.models import InstrumentRiskSnapshot, MarginModelResult, RiskEngineError


def _resolve_rates(
    instrument: InstrumentRiskSnapshot, settings: RiskSettings
) -> tuple[float, float, bool]:
    """(initial_rate, maintenance_rate, approximated) or raise."""
    imr = instrument.initial_margin_rate
    mmr = instrument.maintenance_margin_rate
    if imr is not None and mmr is not None:
        if not (0 < mmr < imr < 1):
            raise RiskEngineError(
                PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE,
                "public margin rates are implausible - refusing the model",
            )
        return imr, mmr, False
    if imr is None and mmr is not None:
        raise RiskEngineError(
            PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE,
            "initial margin rate not publicly available",
        )
    if imr is not None and mmr is None:
        raise RiskEngineError(
            PlanRejectionCode.MAINTENANCE_MARGIN_UNAVAILABLE,
            "maintenance margin rate not publicly available",
        )
    if settings.require_instrument_margin_data and not settings.allow_approximated_margin_model:
        raise RiskEngineError(
            PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE,
            "no public margin information for this instrument and the "
            "approximated model is not enabled (default: block)",
        )
    if not settings.allow_approximated_margin_model:
        raise RiskEngineError(
            PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE,
            "approximated margin model disabled",
        )
    approx_imr = settings.approx_initial_margin_rate
    approx_mmr = settings.approx_maintenance_margin_rate
    if not (0 < approx_mmr < approx_imr < 1):
        raise RiskEngineError(
            PlanRejectionCode.CONFIGURATION_INVALID,
            "approximated margin rates must satisfy 0 < maintenance < initial < 1",
        )
    return approx_imr, approx_mmr, True


def build_margin_model(
    instrument: InstrumentRiskSnapshot,
    *,
    entry_reference_price: float,
    notional: float,
    leverage: float,
    bullish: bool,
    settings: RiskSettings,
) -> MarginModelResult:
    """Hypothetical isolated-margin figures for one reference leverage.

    The liquidation threshold uses the classic isolated approximation
    ``entry * (1 -/+ 1/L +/- mmr)`` shifted by the stress buffer TOWARDS the
    entry, and takes the more conservative of entry- and mark-anchored
    values.  It is a research plausibility bound, never a guarantee.
    """
    if not settings.isolated_margin_only:
        raise RiskEngineError(
            PlanRejectionCode.CONFIGURATION_INVALID,
            "only hypothetical isolated-margin research is supported",
        )
    if leverage < 1.0:
        raise RiskEngineError(
            PlanRejectionCode.CONFIGURATION_INVALID, "reference leverage below 1x"
        )
    mark = instrument.mark_price
    if mark is None or mark <= 0:
        raise RiskEngineError(
            PlanRejectionCode.INSTRUMENT_RISK_DATA_MISSING,
            "no mark price - margin plausibility needs the mark reference",
        )
    imr, mmr, approximated = _resolve_rates(instrument, settings)
    if leverage > 1.0 / imr:
        raise RiskEngineError(
            PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE,
            f"leverage {leverage:g}x exceeds the initial-margin bound {1.0 / imr:.2f}x",
        )
    required_initial = max(notional / leverage, notional * imr)
    maintenance = notional * mmr
    stress = settings.margin_stress_buffer_bps / 10_000

    def liquidation_from(anchor: float) -> float:
        if bullish:
            return anchor * (1.0 - 1.0 / leverage + mmr + stress)
        return anchor * (1.0 + 1.0 / leverage - mmr - stress)

    liq_entry = liquidation_from(entry_reference_price)
    liq_mark = liquidation_from(mark)
    # conservative: the threshold closer to the position
    liquidation = max(liq_entry, liq_mark) if bullish else min(liq_entry, liq_mark)
    flags: list[str] = []
    if approximated:
        flags = [
            "APPROXIMATED_MARGIN_MODEL",
            f"APPROX_INITIAL_MARGIN_RATE={imr:g}",
            f"APPROX_MAINTENANCE_MARGIN_RATE={mmr:g}",
        ]
    return MarginModelResult(
        mode=MarginModelMode.APPROXIMATED if approximated else MarginModelMode.PUBLIC_RATES,
        approximated=approximated,
        initial_margin_rate=imr,
        maintenance_margin_rate=mmr,
        required_initial_margin_estimate=required_initial,
        maintenance_margin_estimate=maintenance,
        hypothetical_liquidation_price=liquidation,
        reference_leverage=leverage,
        mark_price_reference=mark,
        approximation_flags=tuple(flags),
        detail=(
            f"isolated research model at {leverage:g}x, "
            f"{'APPROXIMATED rates' if approximated else 'public rates'}, "
            f"mark reference {mark:.6g}; conservative plausibility bound, "
            "no liquidation guarantee"
        ),
    )
