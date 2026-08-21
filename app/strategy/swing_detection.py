"""Confirmed pivot-based swing detection.

A pivot high at index i requires ``left`` candles before and ``right``
candles after with strictly lower highs (mirrored for lows).  The swing is
only CONFIRMED once all ``right`` candles have CLOSED - the confirmation time
is the close time of the last required candle, so no future knowledge is
ever used at live-equivalent evaluation time.

Optionally, swings with prominence below ``atr_multiplier * ATR`` are
filtered as noise.
"""

from __future__ import annotations

from app.strategy.candle_features import atr
from app.strategy.enums import SwingType
from app.strategy.models import CandleSeries, Swing


def detect_swings(
    series: CandleSeries,
    *,
    left_bars: int,
    right_bars: int,
    atr_multiplier: float,
    atr_period: int,
) -> list[Swing]:
    candles = series.candles
    if len(candles) < left_bars + right_bars + 1:
        return []
    atr_value = atr(series, atr_period) or 0.0
    min_prominence = atr_value * atr_multiplier
    swings: list[Swing] = []

    for index in range(left_bars, len(candles) - right_bars):
        pivot = candles[index]
        left = candles[index - left_bars : index]
        right = candles[index + 1 : index + 1 + right_bars]
        confirm_candle = right[-1]

        if all(candle.high < pivot.high for candle in left) and all(
            candle.high < pivot.high for candle in right
        ):
            prominence = pivot.high - max(
                min(candle.low for candle in left), min(candle.low for candle in right)
            )
            if min_prominence and prominence < min_prominence:
                continue
            swings.append(
                Swing(
                    swing_id=f"{series.timeframe.value}:H:{pivot.open_time.isoformat()}",
                    timeframe=series.timeframe,
                    swing_type=SwingType.HIGH,
                    price=pivot.high,
                    candle_open_time=pivot.open_time,
                    confirmed_at=confirm_candle.close_time,
                    strength=prominence / atr_value if atr_value else 1.0,
                    relevance=1.0,
                    params={"left": left_bars, "right": right_bars},
                )
            )
        if all(candle.low > pivot.low for candle in left) and all(
            candle.low > pivot.low for candle in right
        ):
            prominence = (
                min(max(candle.high for candle in left), max(candle.high for candle in right))
                - pivot.low
            )
            if min_prominence and prominence < min_prominence:
                continue
            swings.append(
                Swing(
                    swing_id=f"{series.timeframe.value}:L:{pivot.open_time.isoformat()}",
                    timeframe=series.timeframe,
                    swing_type=SwingType.LOW,
                    price=pivot.low,
                    candle_open_time=pivot.open_time,
                    confirmed_at=confirm_candle.close_time,
                    strength=prominence / atr_value if atr_value else 1.0,
                    relevance=1.0,
                    params={"left": left_bars, "right": right_bars},
                )
            )
    swings.sort(key=lambda swing: swing.candle_open_time)
    # recency-weighted relevance (most recent swings matter most)
    total = len(swings)
    return [
        Swing(
            swing_id=swing.swing_id,
            timeframe=swing.timeframe,
            swing_type=swing.swing_type,
            price=swing.price,
            candle_open_time=swing.candle_open_time,
            confirmed_at=swing.confirmed_at,
            strength=swing.strength,
            relevance=(index + 1) / total,
            params=swing.params,
        )
        for index, swing in enumerate(swings)
    ]
