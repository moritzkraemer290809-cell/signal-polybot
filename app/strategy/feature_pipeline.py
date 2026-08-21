"""Deterministic feature pipeline: validated series -> full feature bundle.

Pure computation over the immutable EvaluationContext.  Validity of every
feature group is tracked explicitly; missing data is never estimated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import StrategySettings
from app.strategy.candle_features import (
    atr_bps,
    close_location_value,
    distance_bps,
    efficiency_ratio,
    log_return,
    range_compression_ratio,
    rolling_high,
    rolling_low,
    simple_returns,
)
from app.strategy.liquidity_pools import detect_liquidity_levels
from app.strategy.liquidity_sweeps import detect_sweeps
from app.strategy.market_context import MarketContextFeatures, compute_market_context
from app.strategy.market_structure import analyze_structure
from app.strategy.models import (
    CandleSeries,
    EvaluationContext,
    LiquidityLevel,
    StructureAnalysis,
    SweepEvent,
)
from app.strategy.momentum import MomentumFeatures, compute_momentum
from app.strategy.orderbook_features import OrderbookFeatures, compute_orderbook_features
from app.strategy.swing_detection import detect_swings
from app.strategy.volatility import VolatilityFeatures, compute_volatility
from app.strategy.volume_confirmation import VolumeFeatures, compute_volume


@dataclass(frozen=True)
class FeatureBundle:
    structure: dict[str, StructureAnalysis]
    volatility_1h: VolatilityFeatures
    volatility_5m: VolatilityFeatures
    momentum_5m: MomentumFeatures
    volume_5m: VolumeFeatures
    orderbook: OrderbookFeatures
    market_context: MarketContextFeatures
    liquidity_levels_15m: tuple[LiquidityLevel, ...]
    sweeps_5m: tuple[SweepEvent, ...]
    feature_values: dict[str, Any] = field(default_factory=dict)
    feature_validity: dict[str, bool] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def _series_features(series: CandleSeries, settings: StrategySettings) -> dict[str, Any]:
    last = series.last
    high = rolling_high(series, 20)
    low = rolling_low(series, 20)
    return {
        "close": last.close,
        "log_return_1": log_return(series, 1),
        "return_5": simple_returns(series, 5),
        "atr_bps": atr_bps(series, settings.atr_period),
        "compression_ratio": range_compression_ratio(series, 5, 20),
        "efficiency_ratio": efficiency_ratio(series, min(30, len(series) - 1)),
        "close_location": close_location_value(last),
        "rolling_high_20": high,
        "rolling_low_20": low,
        "dist_to_high_bps": distance_bps(last.close, high) if high else None,
        "dist_to_low_bps": distance_bps(last.close, low) if low else None,
        "volume": last.volume,
        "trade_count": last.trade_count,
    }


def build_feature_bundle(context: EvaluationContext, settings: StrategySettings) -> FeatureBundle:
    warnings: list[str] = []
    structure: dict[str, StructureAnalysis] = {}
    for timeframe_value in ("1h", "15m", "5m"):
        series = context.series[timeframe_value]
        swings = detect_swings(
            series,
            left_bars=settings.swing_left_bars,
            right_bars=settings.swing_right_bars,
            atr_multiplier=settings.swing_atr_multiplier,
            atr_period=settings.atr_period,
        )
        structure[timeframe_value] = analyze_structure(
            series,
            swings,
            equal_tolerance_bps=settings.equal_level_tolerance_bps,
            confirmation_closes=settings.structure_break_confirmation_closes,
        )

    series_1h = context.series["1h"]
    series_5m = context.series["5m"]
    series_15m = context.series["15m"]

    volatility_1h = compute_volatility(
        series_1h,
        atr_period=settings.atr_period,
        lookback=settings.volatility_lookback,
        high_percentile=settings.high_volatility_percentile,
    )
    volatility_5m = compute_volatility(
        series_5m,
        atr_period=settings.atr_period,
        lookback=settings.volatility_lookback,
        high_percentile=settings.high_volatility_percentile,
    )
    momentum_5m = compute_momentum(series_5m, lookback=settings.momentum_lookback)
    volume_5m = compute_volume(series_5m, lookback=settings.volume_lookback)
    orderbook = compute_orderbook_features(context.market)
    market_context = compute_market_context(context.market)

    levels_15m = detect_liquidity_levels(
        series_15m,
        list(structure["15m"].swings),
        lookback=settings.liquidity_level_lookback,
        equal_tolerance_bps=settings.equal_level_tolerance_bps,
    )
    sweeps_5m = detect_sweeps(
        series_5m, levels_15m, min_overshoot_bps=settings.sweep_min_overshoot_bps
    )

    if not volume_5m.volume_available:
        warnings.append("5m volume baseline unavailable")
    if not orderbook.valid:
        warnings.append("orderbook features invalid (not fresh)")
    if not market_context.valid:
        warnings.append("market context features incomplete")

    feature_values: dict[str, Any] = {
        f"{timeframe}.{key}": value
        for timeframe, series in (("1h", series_1h), ("15m", series_15m), ("5m", series_5m))
        for key, value in _series_features(series, settings).items()
    }
    feature_values.update(
        {
            "5m.momentum_direction": momentum_5m.direction,
            "5m.relative_volume": volume_5m.relative_volume,
            "1h.atr_percentile": volatility_1h.atr_percentile,
            "book.spread_bps": orderbook.spread_bps,
            "book.depth_imbalance": orderbook.depth_imbalance,
            "context.mark_to_index_bps": market_context.mark_to_index_bps,
            "context.mark_to_mid_bps": market_context.mark_to_mid_bps,
            "context.funding_rate": market_context.funding_rate,
        }
    )
    feature_validity = {
        "structure_1h": structure["1h"].state.value != "INSUFFICIENT_STRUCTURE",
        "structure_15m": structure["15m"].state.value != "INSUFFICIENT_STRUCTURE",
        "structure_5m": structure["5m"].state.value != "INSUFFICIENT_STRUCTURE",
        "volatility_1h": volatility_1h.atr_percentile is not None,
        "momentum_5m": momentum_5m.rate_of_change is not None,
        "volume_5m": volume_5m.volume_available,
        "orderbook": orderbook.valid,
        "market_context": market_context.valid,
    }
    return FeatureBundle(
        structure=structure,
        volatility_1h=volatility_1h,
        volatility_5m=volatility_5m,
        momentum_5m=momentum_5m,
        volume_5m=volume_5m,
        orderbook=orderbook,
        market_context=market_context,
        liquidity_levels_15m=tuple(levels_15m),
        sweeps_5m=tuple(sweeps_5m),
        feature_values=feature_values,
        feature_validity=feature_validity,
        warnings=tuple(warnings),
    )
