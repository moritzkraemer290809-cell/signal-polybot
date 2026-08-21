"""Momentum confirmation features - never standalone signals."""

from __future__ import annotations

from dataclasses import dataclass

from app.strategy.candle_features import body_to_range_ratio, close_location_value, simple_returns
from app.strategy.models import CandleSeries


@dataclass(frozen=True)
class MomentumFeatures:
    rate_of_change: float | None
    close_to_close: float | None
    last_body_ratio: float | None
    last_close_location: float | None
    continuation_count: int
    rsi: float | None
    direction: int  # +1 bullish momentum, -1 bearish, 0 neutral/unknown
    detail: str


def _rsi(series: CandleSeries, period: int) -> float | None:
    candles = series.candles
    if len(candles) < period + 1:
        return None
    gains = losses = 0.0
    for previous, current in zip(candles[-period - 1 : -1], candles[-period:], strict=True):
        delta = current.close - previous.close
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def compute_momentum(series: CandleSeries, *, lookback: int) -> MomentumFeatures:
    roc = simple_returns(series, lookback)
    close_to_close = simple_returns(series, 1)
    last = series.last
    body_ratio = body_to_range_ratio(last)
    close_location = close_location_value(last)

    # consecutive same-direction closes ending at the last candle
    continuation = 0
    candles = series.candles
    if len(candles) >= 2:
        direction_last = (candles[-1].close > candles[-2].close) - (
            candles[-1].close < candles[-2].close
        )
        if direction_last != 0:
            continuation = 1
            for index in range(len(candles) - 2, 0, -1):
                step = (candles[index].close > candles[index - 1].close) - (
                    candles[index].close < candles[index - 1].close
                )
                if step == direction_last:
                    continuation += 1
                else:
                    break

    rsi = _rsi(series, 14)
    direction = 0
    if roc is not None and body_ratio is not None:
        if roc > 0 and (close_location or 0) > 0:
            direction = 1
        elif roc < 0 and (close_location or 0) < 0:
            direction = -1
    return MomentumFeatures(
        rate_of_change=roc,
        close_to_close=close_to_close,
        last_body_ratio=body_ratio,
        last_close_location=close_location,
        continuation_count=continuation,
        rsi=rsi,
        direction=direction,
        detail=f"roc={roc:.5f}" if roc is not None else "roc=n/a",
    )
