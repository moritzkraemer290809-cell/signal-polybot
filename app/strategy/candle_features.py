"""Pure candle/price feature functions over validated CandleSeries.

All functions are deterministic and operate only on closed candles; a feature
for candle i uses candles [.. i] only - never later ones.
"""

from __future__ import annotations

import itertools
import math

from app.strategy.models import Candle, CandleSeries


def simple_returns(series: CandleSeries, window: int) -> float | None:
    candles = series.candles
    if len(candles) <= window or candles[-1 - window].close == 0:
        return None
    return candles[-1].close / candles[-1 - window].close - 1.0


def log_return(series: CandleSeries, window: int = 1) -> float | None:
    candles = series.candles
    if len(candles) <= window:
        return None
    previous, current = candles[-1 - window].close, candles[-1].close
    if previous <= 0 or current <= 0:
        return None
    return math.log(current / previous)


def true_range(previous: Candle | None, current: Candle) -> float:
    if previous is None:
        return current.range
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def true_range_series(series: CandleSeries) -> list[float]:
    result: list[float] = []
    previous: Candle | None = None
    for candle in series.candles:
        result.append(true_range(previous, candle))
        previous = candle
    return result


def atr(series: CandleSeries, period: int) -> float | None:
    """Wilder-smoothed ATR over closed candles."""
    ranges = true_range_series(series)
    if len(ranges) < period + 1:
        return None
    value = sum(ranges[1 : period + 1]) / period
    for tr_value in ranges[period + 1 :]:
        value = (value * (period - 1) + tr_value) / period
    return value


def atr_bps(series: CandleSeries, period: int) -> float | None:
    value = atr(series, period)
    close = series.last.close
    if value is None or close <= 0:
        return None
    return value / close * 10_000


def realized_volatility(series: CandleSeries, window: int) -> float | None:
    """Stddev of 1-candle log returns over the window (per-candle vol)."""
    candles = series.candles
    if len(candles) < window + 1:
        return None
    returns: list[float] = []
    for previous, current in zip(candles[-window - 1 : -1], candles[-window:], strict=True):
        if previous.close <= 0 or current.close <= 0:
            return None
        returns.append(math.log(current.close / previous.close))
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    return math.sqrt(variance)


def body_to_range_ratio(candle: Candle) -> float | None:
    if candle.range == 0:
        return None
    return candle.body / candle.range


def upper_wick_ratio(candle: Candle) -> float | None:
    if candle.range == 0:
        return None
    return (candle.high - max(candle.open, candle.close)) / candle.range


def lower_wick_ratio(candle: Candle) -> float | None:
    if candle.range == 0:
        return None
    return (min(candle.open, candle.close) - candle.low) / candle.range


def close_location_value(candle: Candle) -> float | None:
    """-1 (close at low) .. +1 (close at high)."""
    if candle.range == 0:
        return None
    return ((candle.close - candle.low) - (candle.high - candle.close)) / candle.range


def rolling_high(series: CandleSeries, window: int, *, exclude_last: bool = False) -> float | None:
    candles = series.candles[:-1] if exclude_last else series.candles
    if len(candles) < window:
        return None
    return max(candle.high for candle in candles[-window:])


def rolling_low(series: CandleSeries, window: int, *, exclude_last: bool = False) -> float | None:
    candles = series.candles[:-1] if exclude_last else series.candles
    if len(candles) < window:
        return None
    return min(candle.low for candle in candles[-window:])


def distance_bps(price: float, reference: float) -> float | None:
    if reference <= 0:
        return None
    return (price - reference) / reference * 10_000


def range_compression_ratio(series: CandleSeries, short: int, long: int) -> float | None:
    """avg range(short) / avg range(long); < 1 = compression, > 1 = expansion."""
    candles = series.candles
    if len(candles) < long or short <= 0 or long <= short:
        return None
    short_avg = sum(candle.range for candle in candles[-short:]) / short
    long_avg = sum(candle.range for candle in candles[-long:]) / long
    if long_avg == 0:
        return None
    return short_avg / long_avg


def efficiency_ratio(series: CandleSeries, window: int) -> float | None:
    """Kaufman efficiency ratio: |net move| / sum of |candle-to-candle moves|."""
    candles = series.candles
    if len(candles) < window + 1:
        return None
    closes = [candle.close for candle in candles[-window - 1 :]]
    net = abs(closes[-1] - closes[0])
    path = sum(abs(b - a) for a, b in itertools.pairwise(closes))
    if path == 0:
        return None
    return net / path


def directional_sign(series: CandleSeries, window: int) -> int:
    """+1/-1/0 net close direction over the window."""
    candles = series.candles
    if len(candles) < window + 1:
        return 0
    delta = candles[-1].close - candles[-1 - window].close
    return (delta > 0) - (delta < 0)


def rolling_mean_volume(
    series: CandleSeries, window: int, *, exclude_last: bool = True
) -> float | None:
    candles = series.candles[:-1] if exclude_last else series.candles
    if len(candles) < window:
        return None
    return sum(candle.volume for candle in candles[-window:]) / window
