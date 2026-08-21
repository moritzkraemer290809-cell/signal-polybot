"""Reclaim and rejection features - close-confirmed only."""

from __future__ import annotations

from app.strategy.enums import SweepDirection
from app.strategy.models import CandleSeries, ReclaimEvent, RejectionEvent, SweepEvent


def detect_reclaim(
    series: CandleSeries, sweep: SweepEvent, *, tolerance_bps: float
) -> ReclaimEvent | None:
    """After a sweep, a candle CLOSE back on the valid side of the level
    (with tolerance) confirms the reclaim."""
    level = sweep.level.price
    if level <= 0:
        return None
    tolerance = level * tolerance_bps / 10_000
    for candle in series.candles:
        if candle.open_time < sweep.sweep_candle_open_time:
            continue
        if sweep.direction is SweepDirection.BELOW:
            # close back above the level, clearing it by the tolerance buffer
            reclaimed = candle.close > level + tolerance
        else:
            reclaimed = candle.close < level - tolerance
        if reclaimed:
            distance_bps = abs(candle.close - level) / level * 10_000
            return ReclaimEvent(
                level_price=level,
                close_price=candle.close,
                distance_bps=distance_bps,
                confirmed_at=candle.close_time,
                confidence=min(0.9, sweep.confidence + 0.2),
                detail=(
                    f"close {'above' if sweep.direction is SweepDirection.BELOW else 'below'} "
                    f"{level:.6g} after sweep"
                ),
            )
    return None


def detect_rejection(
    series: CandleSeries,
    level_price: float,
    *,
    direction_bearish: bool,
    min_wick_ratio: float = 0.5,
) -> RejectionEvent | None:
    """Rejection at a level: pronounced wick into the level, close away from
    it, plus a following closed candle continuing away (never wick-only)."""
    if level_price <= 0 or len(series.candles) < 2:
        return None
    for index in range(len(series.candles) - 1):
        candle = series.candles[index]
        follow = series.candles[index + 1]
        if candle.range == 0:
            continue
        if direction_bearish:
            touched = candle.high >= level_price
            wick_ratio = (candle.high - max(candle.open, candle.close)) / candle.range
            closed_away = candle.close < level_price
            followed = follow.close < candle.close
            close_position = (candle.close - candle.low) / candle.range
        else:
            touched = candle.low <= level_price
            wick_ratio = (min(candle.open, candle.close) - candle.low) / candle.range
            closed_away = candle.close > level_price
            followed = follow.close > candle.close
            close_position = (candle.close - candle.low) / candle.range
        if touched and wick_ratio >= min_wick_ratio and closed_away and followed:
            return RejectionEvent(
                level_price=level_price,
                wick_ratio=wick_ratio,
                close_position=close_position,
                followed_through=True,
                confirmed_at=follow.close_time,
                confidence=0.8,
                detail=(f"wick_ratio={wick_ratio:.2f} at {level_price:.6g} with follow-through"),
            )
    return None
