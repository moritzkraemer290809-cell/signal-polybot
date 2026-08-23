"""Conservative leverage suitability range.

The engine never claims an "optimal" leverage.  It derives a conservative
suitability range bounded by the V1 hard cap (3x), the instrument's market
maximum, the notional risk tier, the margin model and the liquidation
buffer, then reduces it further under elevated volatility, wide spreads or
thin funding data.  Score never raises leverage.
"""

from __future__ import annotations

from app.config import RiskSettings
from app.risk.enums import PlanRejectionCode
from app.risk.liquidation_buffer import check_liquidation_buffer
from app.risk.margin_model import build_margin_model
from app.risk.models import (
    InstrumentRiskSnapshot,
    LeverageSuitabilityRange,
    LiquidationBufferResult,
    MarginModelResult,
    RiskEngineError,
)

_STEP = 0.5


def _tier_cap(instrument: InstrumentRiskSnapshot, notional: float) -> int | None:
    """Max leverage of the risk tier matching the reference notional."""
    applicable: int | None = None
    best_bound = -1.0
    for tier in instrument.risk_tiers:
        try:
            bound = float(tier.get("lower_bound", 0))
            tier_max = int(tier.get("max_leverage", 0))
        except (TypeError, ValueError):
            continue
        if notional >= bound and bound >= best_bound and tier_max > 0:
            best_bound = bound
            applicable = tier_max
    return applicable


def determine_leverage_suitability(
    instrument: InstrumentRiskSnapshot,
    *,
    entry_reference_price: float,
    invalidation_price: float,
    notional: float,
    bullish: bool,
    atr: float | None,
    atr_percentile: float | None,
    spread_bps: float | None,
    funding_thin: bool,
    settings: RiskSettings,
) -> tuple[LeverageSuitabilityRange, MarginModelResult, LiquidationBufferResult]:
    """Largest conservative reference leverage whose liquidation buffer holds.

    Raises with a structured code when no leverage down to 1x satisfies the
    buffer, or when required instrument/margin data is missing.
    """
    max_market = instrument.max_leverage
    if max_market is None or max_market < 1:
        raise RiskEngineError(
            PlanRejectionCode.MAX_LEVERAGE_UNAVAILABLE,
            "instrument max leverage unavailable or implausible",
        )
    reasons: list[str] = [f"V1 hard research cap {settings.max_reference_leverage:g}x"]
    cap = min(settings.max_reference_leverage, float(max_market))
    if float(max_market) < settings.max_reference_leverage:
        reasons.append(f"instrument market cap {max_market}x")
    tier_max = _tier_cap(instrument, notional)
    if tier_max is not None and tier_max < cap:
        cap = float(tier_max)
        reasons.append(f"risk tier cap {tier_max}x at notional {notional:.0f} pUSD")
    if atr_percentile is not None and atr_percentile >= 75:
        reduced = max(1.0, cap - 1.0)
        if reduced < cap:
            cap = reduced
            reasons.append(f"elevated volatility (ATR pctl {atr_percentile:.0f}) reduces cap")
    if spread_bps is not None and spread_bps > 10:
        reduced = max(1.0, cap - 0.5)
        if reduced < cap:
            cap = reduced
            reasons.append(f"wide spread ({spread_bps:.1f}bps) reduces cap")
    if funding_thin:
        reduced = max(1.0, cap - 0.5)
        if reduced < cap:
            cap = reduced
            reasons.append("thin funding data reduces cap")

    candidates: list[float] = []
    level = cap
    while level >= 1.0 - 1e-9:
        candidates.append(round(level, 2))
        level -= _STEP
    if not candidates or candidates[-1] > 1.0:
        candidates.append(1.0)

    margin_error: RiskEngineError | None = None
    last_margin: MarginModelResult | None = None
    last_buffer: LiquidationBufferResult | None = None
    for leverage in candidates:
        try:
            margin = build_margin_model(
                instrument,
                entry_reference_price=entry_reference_price,
                notional=notional,
                leverage=leverage,
                bullish=bullish,
                settings=settings,
            )
        except RiskEngineError as error:
            if (
                error.code
                in (
                    PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE,
                    PlanRejectionCode.MAINTENANCE_MARGIN_UNAVAILABLE,
                    PlanRejectionCode.INSTRUMENT_RISK_DATA_MISSING,
                    PlanRejectionCode.CONFIGURATION_INVALID,
                )
                and "exceeds the initial-margin bound" not in error.detail
            ):
                raise
            margin_error = error
            continue
        buffer = check_liquidation_buffer(
            invalidation_price=invalidation_price,
            margin=margin,
            atr=atr,
            bullish=bullish,
            settings=settings,
        )
        last_margin, last_buffer = margin, buffer
        if buffer.sufficient:
            suitability = LeverageSuitabilityRange(
                allowed_leverage_min=1.0,
                allowed_leverage_max=leverage,
                recommended_reference_leverage=leverage,
                max_market_leverage=max_market,
                required_initial_margin_estimate=margin.required_initial_margin_estimate,
                maintenance_margin_estimate=margin.maintenance_margin_estimate,
                liquidation_buffer_bps=buffer.buffer_bps,
                suitability_reasons=tuple([*reasons, f"liquidation buffer holds at {leverage:g}x"]),
                model_confidence=0.5 if margin.approximated else 0.8,
                approximation_flags=margin.approximation_flags,
            )
            return suitability, margin, buffer
    if last_margin is None or last_buffer is None:
        raise margin_error or RiskEngineError(
            PlanRejectionCode.MARGIN_MODEL_UNAVAILABLE, "no margin model could be built"
        )
    raise RiskEngineError(
        PlanRejectionCode.LIQUIDATION_BUFFER_INSUFFICIENT,
        f"even at 1x the invalidation sits too close to the hypothetical "
        f"liquidation threshold ({last_buffer.detail})",
    )
