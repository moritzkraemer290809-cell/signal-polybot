"""Volume/trade-intensity confirmation features with explicit missingness."""

from __future__ import annotations

from dataclasses import dataclass

from app.strategy.candle_features import rolling_mean_volume
from app.strategy.models import CandleSeries


@dataclass(frozen=True)
class VolumeFeatures:
    relative_volume: float | None
    relative_trade_count: float | None
    volume_available: bool
    trade_count_available: bool
    detail: str


def compute_volume(series: CandleSeries, *, lookback: int) -> VolumeFeatures:
    last = series.last
    baseline = rolling_mean_volume(series, lookback, exclude_last=True)
    volume_available = baseline is not None and baseline > 0 and last.volume >= 0
    relative_volume = last.volume / baseline if volume_available and baseline else None

    candles = series.candles[:-1]
    trade_baseline = (
        sum(candle.trade_count for candle in candles[-lookback:]) / lookback
        if len(candles) >= lookback
        else None
    )
    trade_count_available = trade_baseline is not None and trade_baseline > 0
    relative_trades = (
        last.trade_count / trade_baseline if trade_count_available and trade_baseline else None
    )
    return VolumeFeatures(
        relative_volume=relative_volume,
        relative_trade_count=relative_trades,
        volume_available=bool(volume_available),
        trade_count_available=bool(trade_count_available),
        detail=(f"rel_vol={relative_volume:.2f}" if relative_volume is not None else "rel_vol=n/a"),
    )
