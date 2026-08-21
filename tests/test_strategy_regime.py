"""Regime classification: all states, conservatism, explainability."""

from __future__ import annotations

from tests.strategy_helpers import build_context, default_series_set

from app.config import StrategySettings
from app.strategy.enums import StrategyRegime
from app.strategy.feature_pipeline import build_feature_bundle
from app.strategy.regime import classify_regime
from app.strategy.strategy_engine import evaluate_context

SETTINGS = StrategySettings(_env_file=None)


def classify(series, as_of, *, low_liquidity=False, degraded=False, settings=SETTINGS):
    context = build_context(series, as_of, settings)
    bundle = build_feature_bundle(context, settings)
    return classify_regime(
        series["1h"],
        bundle.structure["1h"],
        bundle.volatility_1h,
        settings,
        as_of=as_of,
        low_liquidity=low_liquidity,
        data_degraded=degraded,
    )


def test_trend_up_and_down() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    result = classify(series, as_of)
    assert result.regime is StrategyRegime.TREND_UP
    assert result.confidence > 50
    assert any("efficiency_ratio" in reason for reason in result.reasons)
    assert result.features_used["structure_state"] == "BULLISH"

    series, as_of = default_series_set(trend_up=False, settings=SETTINGS)
    assert classify(series, as_of).regime is StrategyRegime.TREND_DOWN


def test_range_regime() -> None:
    series, as_of = default_series_set(trend_up=None, settings=SETTINGS)
    result = classify(series, as_of)
    assert result.regime is StrategyRegime.RANGE


def test_low_liquidity_overrides_trend() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    result = classify(series, as_of, low_liquidity=True)
    assert result.regime is StrategyRegime.LOW_LIQUIDITY


def test_high_volatility_overrides_trend() -> None:
    from tests.strategy_helpers import build_series, make_candle, trending_candles

    from app.domain.enums import Timeframe

    candles = trending_candles(Timeframe.H1, 39, up=True, step=0.8)
    last_close = candles[-1].close
    shock = [
        make_candle(Timeframe.H1, 39, last_close, last_close + 12, last_close - 12, last_close + 1),
        make_candle(Timeframe.H1, 40, last_close + 1, last_close + 14, last_close - 10, last_close),
    ]
    series, _ = default_series_set(trend_up=True, settings=SETTINGS)
    all_candles = [*candles, *shock]
    series = dict(series)
    series["1h"] = build_series(Timeframe.H1, all_candles, all_candles[-1].close_time, SETTINGS)
    result = classify(series, all_candles[-1].close_time)
    assert result.regime is StrategyRegime.HIGH_VOLATILITY


def test_event_risk_window() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    settings = StrategySettings(
        _env_file=None,
        event_risk_enabled=True,
        event_risk_windows_json=[
            {
                "start": (as_of.replace(microsecond=0)).isoformat(),
                "end": (as_of.replace(microsecond=0)).isoformat().replace("+00:00", "") + "+02:00",
                "label": "TEST-EVENT",
            }
        ],
    )
    # simpler deterministic window around as_of
    settings = StrategySettings(
        _env_file=None,
        event_risk_enabled=True,
        event_risk_windows_json=[
            {"start": "2026-08-01T00:00:00Z", "end": "2026-12-31T00:00:00Z", "label": "T"}
        ],
    )
    result = classify(series, as_of, settings=settings)
    assert result.regime is StrategyRegime.EVENT_RISK
    # disabled by default -> no effect
    assert classify(series, as_of).regime is not StrategyRegime.EVENT_RISK


def test_insufficient_data() -> None:
    from tests.strategy_helpers import trending_candles

    from app.domain.enums import Timeframe
    from app.strategy.models import CandleSeries

    short = trending_candles(Timeframe.H1, 4)  # too few for percentile history
    series_1h = CandleSeries.build(
        Timeframe.H1, short, short[-1].close_time, min_candles=1, max_gap_multiplier=1.5
    )
    series, _ = default_series_set(trend_up=True, settings=SETTINGS)
    series = dict(series)
    series["1h"] = series_1h
    result = classify(series, short[-1].close_time)
    assert result.regime is StrategyRegime.INSUFFICIENT_DATA


def test_contradiction_yields_no_trade() -> None:
    """Bullish drift with confirmed bearish structure -> NO_TRADE."""
    from tests.strategy_helpers import build_series, make_candle, trending_candles

    from app.domain.enums import Timeframe

    # downtrend structure, then a monotone drift up that stays BELOW the last
    # confirmed swing high (no BOS/ChoCh) - structure remains bearish
    down = trending_candles(Timeframe.H1, 34, up=False, start_price=300.0, step=1.0)
    last = down[-1].close
    drift = [
        make_candle(
            Timeframe.H1,
            34 + i,
            last + i * 0.5,
            last + (i + 1) * 0.5 + 0.05,
            last + i * 0.5 - 0.05,
            last + (i + 1) * 0.5,
        )
        for i in range(7)
    ]
    candles = [*down, *drift]
    series, _ = default_series_set(trend_up=False, settings=SETTINGS)
    series = dict(series)
    series["1h"] = build_series(Timeframe.H1, candles, candles[-1].close_time, SETTINGS)
    # short trend lookback: the drift dominates direction, structure stays bearish
    settings = StrategySettings(
        _env_file=None,
        regime_rules_json={
            "trend_efficiency_ratio_min": 0.35,
            "range_efficiency_ratio_max": 0.2,
            "breakout_lookback": 40,  # wide: drift stays inside prior range
            "trend_lookback": 6,
            "low_liquidity_quality_below": 60,
        },
    )
    result = classify(series, candles[-1].close_time, settings=settings)
    assert result.regime is StrategyRegime.NO_TRADE
    assert any("contradicts" in reason for reason in result.reasons)


def test_breakout_requires_close_outside_range() -> None:
    from tests.strategy_helpers import build_series, make_candle, ranging_candles

    from app.domain.enums import Timeframe

    base = ranging_candles(Timeframe.H1, 40, center=100.0, amplitude=1.0)
    # heterogeneous history: a few wide-range candles (extended lows only, so
    # the prior range HIGH is untouched) keep the breakout candle's true range
    # below the high-volatility percentile
    for index in (5, 12, 19, 26, 33):
        candle = base[index]
        base[index] = make_candle(
            Timeframe.H1, index, candle.open, candle.high, candle.low - 1.5, candle.close
        )
    breakout = make_candle(Timeframe.H1, 40, base[-1].close, 101.6, base[-1].close - 0.1, 101.5)
    candles = [*base, breakout]
    series, _ = default_series_set(trend_up=None, settings=SETTINGS)
    series = dict(series)
    series["1h"] = build_series(Timeframe.H1, candles, candles[-1].close_time, SETTINGS)
    result = classify(series, candles[-1].close_time)
    assert result.regime is StrategyRegime.BREAKOUT

    # intrabar spike WITHOUT closing outside stays non-breakout
    wick_only = [
        *base,
        make_candle(Timeframe.H1, 40, base[-1].close, 101.6, base[-1].close - 0.1, 100.6),
    ]
    series["1h"] = build_series(Timeframe.H1, wick_only, wick_only[-1].close_time, SETTINGS)
    result = classify(series, wick_only[-1].close_time)
    assert result.regime is not StrategyRegime.BREAKOUT


def test_regime_reasons_are_always_present() -> None:
    for trend in (True, False, None):
        series, as_of = default_series_set(trend_up=trend, settings=SETTINGS)
        result = classify(series, as_of)
        assert result.reasons
        assert result.features_used


def test_regime_blocks_produce_rejections_in_engine() -> None:
    series, as_of = default_series_set(trend_up=True, settings=SETTINGS)
    context = build_context(series, as_of, SETTINGS, low_liquidity=True)
    outcome = evaluate_context(context, SETTINGS)
    assert outcome.candidates == ()
    assert outcome.rejections[0].primary_code.value == "REGIME_LOW_LIQUIDITY"
