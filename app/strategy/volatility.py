"""Volatility features: ATR context, percentiles, expansion/compression."""

from __future__ import annotations

from dataclasses import dataclass

from app.strategy.candle_features import (
    atr,
    atr_bps,
    range_compression_ratio,
    realized_volatility,
    true_range_series,
)
from app.strategy.models import CandleSeries


@dataclass(frozen=True)
class VolatilityFeatures:
    atr_value: float | None
    atr_bps: float | None
    realized_vol: float | None
    atr_percentile: float | None
    compression_ratio: float | None
    is_high_volatility: bool
    is_compressed: bool
    detail: str


def _percentile_rank(values: list[float], value: float) -> float:
    """Midrank percentile of value within values (0..100), deterministic.

    Uses (less + equal/2) / n so a uniform distribution ranks at 50 instead
    of a degenerate 100."""
    if not values:
        return 0.0
    less = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    return (less + equal / 2.0) / len(values) * 100.0


def compute_volatility(
    series: CandleSeries,
    *,
    atr_period: int,
    lookback: int,
    high_percentile: float,
) -> VolatilityFeatures:
    atr_value = atr(series, atr_period)
    atr_in_bps = atr_bps(series, atr_period)
    realized = realized_volatility(series, min(lookback, max(len(series) - 1, 2)))
    compression = range_compression_ratio(series, short=5, long=20)

    # ATR percentile vs the instrument's own recent true-range distribution
    ranges = true_range_series(series)
    history = ranges[-lookback:] if len(ranges) >= 5 else []
    current_tr = ranges[-1] if ranges else 0.0
    percentile = _percentile_rank(history, current_tr) if history else None

    is_high = percentile is not None and percentile >= high_percentile
    is_compressed = compression is not None and compression < 0.7
    return VolatilityFeatures(
        atr_value=atr_value,
        atr_bps=atr_in_bps,
        realized_vol=realized,
        atr_percentile=percentile,
        compression_ratio=compression,
        is_high_volatility=is_high,
        is_compressed=is_compressed,
        detail=(f"atr_pctl={percentile:.0f}" if percentile is not None else "atr_pctl=n/a"),
    )
