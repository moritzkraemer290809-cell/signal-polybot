"""Deterministic liquidity pool identification from confirmed swings.

Candle-resolution note: pools are derived from confirmed swing highs/lows,
equal levels and rolling range extremes.  Intrabar order is not observable
at candle resolution; downstream sweep detection therefore reports reduced
confidence instead of false certainty (documented in docs/strategy.md).
"""

from __future__ import annotations

from app.strategy.candle_features import rolling_high, rolling_low
from app.strategy.enums import LiquidityLevelType, SwingType
from app.strategy.models import CandleSeries, LiquidityLevel, Swing


def detect_liquidity_levels(
    series: CandleSeries,
    swings: list[Swing],
    *,
    lookback: int,
    equal_tolerance_bps: float,
) -> list[LiquidityLevel]:
    levels: list[LiquidityLevel] = []
    highs = [swing for swing in swings if swing.swing_type is SwingType.HIGH][-6:]
    lows = [swing for swing in swings if swing.swing_type is SwingType.LOW][-6:]

    for swing in highs:
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.SWING_HIGH,
                timeframe=series.timeframe,
                price=swing.price,
                relevance=swing.relevance,
                touches=1,
                is_major=swing.strength >= 1.0,
                detail=f"swing high {swing.swing_id}",
            )
        )
    for swing in lows:
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.SWING_LOW,
                timeframe=series.timeframe,
                price=swing.price,
                relevance=swing.relevance,
                touches=1,
                is_major=swing.strength >= 1.0,
                detail=f"swing low {swing.swing_id}",
            )
        )

    # equal highs/lows: clusters of swings within tolerance -> stronger pools
    def clusters(sequence: list[Swing], level_type: LiquidityLevelType) -> None:
        for anchor in sequence:
            group = [
                other
                for other in sequence
                if other is not anchor
                and anchor.price > 0
                and abs(other.price - anchor.price) / anchor.price * 10_000 <= equal_tolerance_bps
            ]
            if group:
                cluster_price = (
                    max(swing.price for swing in [anchor, *group])
                    if level_type is LiquidityLevelType.EQUAL_HIGHS
                    else min(swing.price for swing in [anchor, *group])
                )
                levels.append(
                    LiquidityLevel(
                        level_type=level_type,
                        timeframe=series.timeframe,
                        price=cluster_price,
                        relevance=min(1.0, 0.5 + 0.25 * len(group)),
                        touches=len(group) + 1,
                        is_major=True,
                        detail=f"{len(group) + 1} equal touches",
                    )
                )
                break  # one cluster per side is enough

    clusters(highs, LiquidityLevelType.EQUAL_HIGHS)
    clusters(lows, LiquidityLevelType.EQUAL_LOWS)

    range_high = rolling_high(series, lookback, exclude_last=True)
    range_low = rolling_low(series, lookback, exclude_last=True)
    if range_high is not None:
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.RANGE_HIGH,
                timeframe=series.timeframe,
                price=range_high,
                relevance=0.8,
                touches=1,
                is_major=True,
                detail=f"rolling {lookback}-candle high",
            )
        )
    if range_low is not None:
        levels.append(
            LiquidityLevel(
                level_type=LiquidityLevelType.RANGE_LOW,
                timeframe=series.timeframe,
                price=range_low,
                relevance=0.8,
                touches=1,
                is_major=True,
                detail=f"rolling {lookback}-candle low",
            )
        )
    return levels
