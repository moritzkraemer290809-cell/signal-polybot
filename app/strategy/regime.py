"""Deterministic, explainable regime classification.

Exactly one primary regime per instrument and as_of, with confidence and
reasons.  Conservative: missing mandatory features -> INSUFFICIENT_DATA;
contradictions -> NO_TRADE; LOW_LIQUIDITY / HIGH_VOLATILITY / EVENT_RISK
override trend labels.  Priority order is fixed and documented below.
"""

from __future__ import annotations

from datetime import datetime

from app.config import StrategySettings
from app.strategy.candle_features import (
    directional_sign,
    efficiency_ratio,
    rolling_high,
    rolling_low,
)
from app.strategy.enums import StrategyRegime, StructureState
from app.strategy.models import CandleSeries, RegimeResult, StructureAnalysis
from app.strategy.volatility import VolatilityFeatures


def _event_risk_active(settings: StrategySettings, as_of: datetime) -> str | None:
    if not settings.event_risk_enabled:
        return None
    for window in settings.event_risk_windows_json:
        try:
            start = datetime.fromisoformat(str(window["start"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(window["end"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if start <= as_of < end:
            return str(window.get("label", "event window"))
    return None


def classify_regime(
    series_1h: CandleSeries,
    structure_1h: StructureAnalysis,
    volatility: VolatilityFeatures,
    settings: StrategySettings,
    *,
    as_of: datetime,
    low_liquidity: bool,
    data_degraded: bool,
) -> RegimeResult:
    rules = settings.regime_rules_json
    trend_lookback = int(rules.get("trend_lookback", 30))
    breakout_lookback = int(rules.get("breakout_lookback", 20))
    er_trend_min = float(rules.get("trend_efficiency_ratio_min", 0.35))
    er_range_max = float(rules.get("range_efficiency_ratio_max", 0.2))

    reasons: list[str] = []
    flags: list[str] = []
    if data_degraded:
        flags.append("DATA_DEGRADED")
    if volatility.is_compressed:
        flags.append("RANGE_COMPRESSION")
    if volatility.atr_percentile is not None and volatility.atr_percentile >= 75:
        flags.append("VOLATILITY_ELEVATED")

    er = efficiency_ratio(series_1h, min(trend_lookback, len(series_1h) - 1))
    direction = directional_sign(series_1h, min(trend_lookback, len(series_1h) - 1))
    features: dict[str, float | str | None] = {
        "efficiency_ratio": er,
        "direction_sign": float(direction),
        "atr_percentile": volatility.atr_percentile,
        "structure_state": structure_1h.state.value,
        "compression_ratio": volatility.compression_ratio,
    }

    def result(regime: StrategyRegime, confidence: int, reason: str) -> RegimeResult:
        reasons.append(reason)
        return RegimeResult(
            regime=regime,
            confidence=max(0, min(100, confidence)),
            secondary_flags=tuple(flags),
            reasons=tuple(reasons),
            features_used=features,
        )

    # 1. mandatory features present?
    if er is None or volatility.atr_percentile is None:
        return result(StrategyRegime.INSUFFICIENT_DATA, 100, "mandatory regime features missing")
    # 2. event risk (manual/calendar-based, default disabled)
    event_label = _event_risk_active(settings, as_of)
    if event_label is not None:
        return result(StrategyRegime.EVENT_RISK, 100, f"configured event window: {event_label}")
    # 3. low liquidity (from selection/data quality, never from price direction)
    if low_liquidity:
        return result(StrategyRegime.LOW_LIQUIDITY, 90, "market quality below liquidity bound")
    # 4. high volatility percentile
    if volatility.is_high_volatility:
        return result(
            StrategyRegime.HIGH_VOLATILITY,
            80,
            f"true-range percentile {volatility.atr_percentile:.0f} >= "
            f"{settings.high_volatility_percentile:.0f}",
        )
    # 5. breakout: last CLOSED candle closes outside the prior range
    prior_high = rolling_high(series_1h, breakout_lookback, exclude_last=True)
    prior_low = rolling_low(series_1h, breakout_lookback, exclude_last=True)
    last_close = series_1h.last.close
    if prior_high is not None and last_close > prior_high:
        return result(StrategyRegime.BREAKOUT, 75, f"close {last_close:.6g} above prior range high")
    if prior_low is not None and last_close < prior_low:
        return result(StrategyRegime.BREAKOUT, 75, f"close {last_close:.6g} below prior range low")
    # 6. trend: efficiency ratio + confirmed structure alignment
    structure = structure_1h.state
    if er >= er_trend_min and direction > 0:
        if structure is StructureState.BEARISH:
            return result(
                StrategyRegime.NO_TRADE, 70, "bullish drift contradicts bearish structure"
            )
        confidence = int(50 + min(45, (er - er_trend_min) * 120))
        return result(
            StrategyRegime.TREND_UP,
            confidence + (10 if structure is StructureState.BULLISH else 0),
            f"efficiency_ratio={er:.2f} with upward drift, structure={structure.value}",
        )
    if er >= er_trend_min and direction < 0:
        if structure is StructureState.BULLISH:
            return result(
                StrategyRegime.NO_TRADE, 70, "bearish drift contradicts bullish structure"
            )
        confidence = int(50 + min(45, (er - er_trend_min) * 120))
        return result(
            StrategyRegime.TREND_DOWN,
            confidence + (10 if structure is StructureState.BEARISH else 0),
            f"efficiency_ratio={er:.2f} with downward drift, structure={structure.value}",
        )
    # 7. range: low efficiency, no breakout
    if er <= er_range_max:
        return result(StrategyRegime.RANGE, 70, f"efficiency_ratio={er:.2f} <= {er_range_max}")
    # 8. in-between: not clearly trending or ranging
    return result(
        StrategyRegime.NO_TRADE,
        60,
        f"efficiency_ratio={er:.2f} between range and trend thresholds",
    )
