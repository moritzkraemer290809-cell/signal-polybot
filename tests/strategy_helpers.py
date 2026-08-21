"""Synthetic candle/context builders for strategy tests.

All data is clearly artificial local test data - no market recordings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import StrategySettings
from app.domain.enums import Timeframe
from app.strategy.models import (
    Candle,
    CandleSeries,
    EvaluationContext,
    MarketContextSnapshot,
)
from app.strategy.version import FEATURE_SCHEMA_VERSION, configuration_hash, ruleset_hash

BASE_TIME = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)


def make_candle(
    timeframe: Timeframe,
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
    trade_count: int = 10,
    start: datetime = BASE_TIME,
) -> Candle:
    return Candle(
        open_time=start + timedelta(milliseconds=timeframe.milliseconds * index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        trade_count=trade_count,
        timeframe=timeframe,
    )


def trending_candles(
    timeframe: Timeframe,
    count: int,
    *,
    start_price: float = 100.0,
    step: float = 0.5,
    up: bool = True,
    wiggle: float = 0.05,
    start: datetime = BASE_TIME,
    volume: float = 100.0,
) -> list[Candle]:
    """Zigzag trend: 4-candle impulse legs, 2-candle pullbacks (net move in
    trend direction) - produces clearly confirmed pivot highs AND lows."""
    candles: list[Candle] = []
    price = start_price
    sign = 1.0 if up else -1.0
    cycle = [step, step, step, step, -step * 0.7, -step * 0.7]
    for index in range(count):
        move = sign * cycle[index % len(cycle)]
        open_ = price
        close = price + move
        # asymmetric wicks so pullback candles never equal the impulse extreme
        # (keeps pivot confirmation strict: neighbours must be strictly lower/higher)
        up_candle = close >= open_
        high = max(open_, close) + (wiggle if up_candle else wiggle * 0.2)
        low = min(open_, close) - (wiggle if not up_candle else wiggle * 0.2)
        candles.append(
            make_candle(timeframe, index, open_, high, low, close, volume=volume, start=start)
        )
        price = close
    return candles


def ranging_candles(
    timeframe: Timeframe,
    count: int,
    *,
    center: float = 100.0,
    amplitude: float = 1.0,
    start: datetime = BASE_TIME,
    volume: float = 100.0,
) -> list[Candle]:
    """Triangle-wave oscillation around center - clear range with pivots."""
    candles: list[Candle] = []
    period = 12

    def level(i: int) -> float:
        phase = i % period
        tri = (phase / (period / 2)) if phase <= period / 2 else (2 - phase / (period / 2))
        return center - amplitude + 2 * amplitude * tri

    for index in range(count):
        open_ = level(index)
        close = level(index + 1)
        high = max(open_, close) + 0.05
        low = min(open_, close) - 0.05
        candles.append(
            make_candle(timeframe, index, open_, high, low, close, volume=volume, start=start)
        )
    return candles


def build_series(
    timeframe: Timeframe,
    candles: list[Candle],
    as_of: datetime,
    settings: StrategySettings | None = None,
) -> CandleSeries:
    settings = settings or StrategySettings(_env_file=None)
    minimums = {
        Timeframe.M1: settings.min_candles_1m,
        Timeframe.M5: settings.min_candles_5m,
        Timeframe.M15: settings.min_candles_15m,
        Timeframe.H1: settings.min_candles_1h,
    }
    return CandleSeries.build(
        timeframe,
        candles,
        as_of,
        min_candles=minimums[timeframe],
        max_gap_multiplier=settings.max_candle_gap_multiplier,
    )


def market_snapshot(as_of: datetime, price: float = 100.0, **overrides) -> MarketContextSnapshot:
    defaults = dict(
        mark_price=price,
        index_price=price * 1.0002,
        last_price=price,
        mid_price=price * 1.0001,
        best_bid=price * 0.9995,
        best_ask=price * 1.0005,
        spread_bps=5.0,
        bid_depth_pusd=20_000.0,
        ask_depth_pusd=20_000.0,
        book_fresh=True,
        bbo_fresh=True,
        funding_rate=0.0001,
        volume_24h_pusd=1_000_000.0,
        snapshot_at=as_of,
    )
    defaults.update(overrides)
    return MarketContextSnapshot(**defaults)


def build_context(
    series: dict[str, CandleSeries],
    as_of: datetime,
    settings: StrategySettings | None = None,
    **overrides,
) -> EvaluationContext:
    settings = settings or StrategySettings(_env_file=None)
    defaults = dict(
        instrument_pk=10,
        instrument_id=1,
        symbol="TEST-PERP",
        asset_class="CRYPTO",
        as_of=as_of,
        series=series,
        market=market_snapshot(as_of, price=series["5m"].last.close if "5m" in series else 100.0),
        session_state="CRYPTO_24_7",
        session_allowed=True,
        watchlist_active=True,
        bot_paused=False,
        data_quality_status="HEALTHY",
        data_quality_ok=True,
        orderbook_ok=True,
        market_quality_score=90,
        low_liquidity=False,
        strategy_name=settings.name,
        strategy_version=settings.version,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        config_hash=configuration_hash(settings),
        ruleset_hash=ruleset_hash(),
    )
    defaults.update(overrides)
    return EvaluationContext(**defaults)


def default_series_set(
    as_of: datetime | None = None,
    *,
    trend_up: bool | None = None,
    settings: StrategySettings | None = None,
) -> tuple[dict[str, CandleSeries], datetime]:
    """1h/15m/5m series ending exactly at a common as_of."""
    settings = settings or StrategySettings(_env_file=None)
    # counts chosen so trend series end inside a pullback leg (no fresh
    # breakout close) -> steady trends classify as TREND_*, not BREAKOUT
    count_1h, count_15m, count_5m = 41, 60, 80
    # align: as_of at the close of the last 1h candle
    end = BASE_TIME + timedelta(hours=count_1h)
    if trend_up is None:
        h1 = ranging_candles(Timeframe.H1, count_1h)
        m15 = ranging_candles(
            Timeframe.M15, count_15m, start=end - timedelta(minutes=15 * count_15m)
        )
        m5 = ranging_candles(Timeframe.M5, count_5m, start=end - timedelta(minutes=5 * count_5m))
    else:
        h1 = trending_candles(Timeframe.H1, count_1h, up=trend_up, step=0.8)
        last_close = h1[-1].close
        m15 = trending_candles(
            Timeframe.M15,
            count_15m,
            up=trend_up,
            step=0.2,
            start_price=last_close - (0.2 * count_15m * (0.52 if trend_up else -0.52)),
            start=end - timedelta(minutes=15 * count_15m),
        )
        m5 = trending_candles(
            Timeframe.M5,
            count_5m,
            up=trend_up,
            step=0.08,
            start_price=m15[-1].close - (0.08 * count_5m * (0.52 if trend_up else -0.52)),
            start=end - timedelta(minutes=5 * count_5m),
        )
    as_of = as_of or end
    return {
        "1h": build_series(Timeframe.H1, h1, as_of, settings),
        "15m": build_series(Timeframe.M15, m15, as_of, settings),
        "5m": build_series(Timeframe.M5, m5, as_of, settings),
    }, as_of
