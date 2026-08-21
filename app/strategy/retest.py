"""Retest feature after a break or reclaim: CONFIRMED / FAILED / PENDING /
NOT_PRESENT within a configurable window and tolerance band."""

from __future__ import annotations

from datetime import datetime

from app.strategy.enums import RetestStatus
from app.strategy.models import CandleSeries, RetestResult


def evaluate_retest(
    series: CandleSeries,
    level_price: float,
    break_time: datetime,
    *,
    bullish: bool,
    tolerance_bps: float,
    max_candles: int,
) -> RetestResult:
    """After a bullish break above ``level_price`` (or bearish below), price
    returning into the tolerance band and holding = CONFIRMED; closing back
    through the level = FAILED; window still open without touch = PENDING."""
    if level_price <= 0:
        return RetestResult(RetestStatus.NOT_PRESENT, None, "invalid level")
    tolerance = level_price * tolerance_bps / 10_000
    window = [candle for candle in series.candles if candle.open_time >= break_time][:max_candles]
    if not window:
        return RetestResult(RetestStatus.PENDING, level_price, "no closed candles after break")
    touched = False
    for candle in window:
        failed = (
            candle.close < level_price - tolerance
            if bullish
            else (candle.close > level_price + tolerance)
        )
        if failed:
            return RetestResult(RetestStatus.FAILED, level_price, "close back through the level")
        touches = (
            candle.low <= level_price + tolerance
            if bullish
            else (candle.high >= level_price - tolerance)
        )
        if touches:
            touched = True
        elif touched:
            # touched the band and moved away again without failing -> held
            return RetestResult(RetestStatus.CONFIRMED, level_price, "retest held")
    if touched:
        return RetestResult(RetestStatus.PENDING, level_price, "retest in progress")
    if len(window) >= max_candles:
        return RetestResult(RetestStatus.NOT_PRESENT, level_price, "window elapsed, no retest")
    return RetestResult(RetestStatus.PENDING, level_price, "awaiting retest window")
