"""Deterministic fixtures for phase-11 simulation and backtest tests.

All data is clearly artificial local test data - no market recordings, no
network access, no account data.  The replay builder emits the same event
vocabulary the production replay consumes, so tests exercise the real
ordering, look-ahead and pipeline code paths.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.strategy_helpers import default_series_set

from app.config import (
    BacktestSettings,
    CostSettings,
    RiskSettings,
    ShadowSimulationSettings,
    SignalLifecycleSettings,
    SimulationReportingSettings,
    StrategySettings,
)
from app.costs.fee_schedule import build_default_schedule, snapshot_from_schedule
from app.costs.models import DepthLevel, FundingSnapshot, OrderbookDepthSnapshot
from app.simulation.enums import ReplayEventCategory, SimulationExitReason
from app.simulation.models import (
    LifecycleObservation,
    MarketReferenceSnapshot,
    ReplayEvent,
    SimulationInputs,
    SimulationPlanReference,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
COST_SETTINGS = CostSettings(_env_file=None)
RISK_SETTINGS = RiskSettings(_env_file=None)
STRATEGY_SETTINGS = StrategySettings(_env_file=None)
LIFECYCLE_SETTINGS = SignalLifecycleSettings(_env_file=None)
REPORTING_SETTINGS = SimulationReportingSettings(_env_file=None)
FEE_SCHEDULE = snapshot_from_schedule(
    build_default_schedule(COST_SETTINGS), COST_SETTINGS, active=True, effective_at=NOW
)


def shadow_settings(**overrides: Any) -> ShadowSimulationSettings:
    defaults: dict[str, Any] = {"mode_enabled": True}
    defaults.update(overrides)
    return ShadowSimulationSettings(_env_file=None, **defaults)


def backtest_settings(**overrides: Any) -> BacktestSettings:
    defaults: dict[str, Any] = {"enabled": True, "require_funding": True}
    defaults.update(overrides)
    return BacktestSettings(_env_file=None, **defaults)


# --------------------------------------------------------------- market data


def depth_book(mid: float, at: datetime, depth: float = 500.0) -> OrderbookDepthSnapshot:
    return OrderbookDepthSnapshot(
        instrument_id=1,
        bids=tuple(DepthLevel(price=mid - 0.05 * i, quantity=depth) for i in range(1, 11)),
        asks=tuple(DepthLevel(price=mid + 0.05 * i, quantity=depth) for i in range(1, 11)),
        fresh=True,
        snapshot_at=at,
    )


def market_reference(
    mid: float,
    at: datetime,
    *,
    depth: float = 500.0,
    staleness_seconds: float = 1.0,
    data_quality_ok: bool = True,
    fresh: bool = True,
) -> MarketReferenceSnapshot:
    stamp = at - timedelta(seconds=staleness_seconds)
    book = depth_book(mid, stamp, depth)
    if not fresh:
        book = OrderbookDepthSnapshot(
            instrument_id=book.instrument_id,
            bids=book.bids,
            asks=book.asks,
            fresh=False,
            snapshot_at=stamp,
        )
    return MarketReferenceSnapshot(
        as_of=at,
        book=book,
        best_bid=mid - 0.05,
        best_ask=mid + 0.05,
        mark_price=mid,
        bbo_at=stamp,
        book_at=stamp,
        data_quality_status="HEALTHY" if data_quality_ok else "DATA_STALE",
        data_quality_ok=data_quality_ok,
        snapshot_id=uuid.uuid4(),
    )


def funding_snapshot(at: datetime = NOW, rate: float = 0.00001) -> FundingSnapshot:
    return FundingSnapshot(
        current_rate=rate,
        history=tuple((at - timedelta(hours=hour), rate) for hour in range(6, 0, -1)),
        next_funding_at=at + timedelta(minutes=30),
        interval_hours=8.0,
        data_fresh=True,
    )


# -------------------------------------------------------------- simulation io


def plan_reference(**overrides: Any) -> SimulationPlanReference:
    defaults: dict[str, Any] = {
        "plan_id": uuid.uuid4(),
        "candidate_id": uuid.uuid4(),
        "instrument_pk": 1,
        "instrument_id": 1,
        "symbol": "BTC-PERP",
        "asset_class": "CRYPTO",
        "candidate_type": "BULLISH_SWEEP_REVERSAL",
        "direction": "BULLISH",
        "entry_reference_price": 100.0,
        "invalidation_price": 97.0,
        "target_prices": (104.0, 107.0),
        "reference_quantity": 10.0,
        "reference_notional": 1000.0,
        "risk_per_unit": 3.0,
        "virtual_account_pusd": 10_000.0,
        "strategy_version": "1.0.0",
        "risk_model_version": "1.0.0",
        "cost_model_version": "1.0.0",
        "fee_schedule_version": FEE_SCHEDULE.version,
        "execution_assumption_version": "taker-conservative-1",
    }
    defaults.update(overrides)
    return SimulationPlanReference(**defaults)


def observation(
    signal_id: uuid.UUID,
    plan: SimulationPlanReference,
    *,
    state: str,
    event_type: str,
    at: datetime,
    state_version: int = 3,
) -> LifecycleObservation:
    return LifecycleObservation(
        signal_id=signal_id,
        plan_id=plan.plan_id,
        candidate_id=plan.candidate_id,
        state=state,
        event_type=event_type,
        state_version=state_version,
        as_of=at,
    )


def simulation_inputs(**overrides: Any) -> SimulationInputs:
    """Default: a bullish lifecycle that reached reference target 1."""
    plan = overrides.pop("plan", None) or plan_reference()
    signal_id = overrides.pop("lifecycle_signal_id", None) or uuid.uuid4()
    entry_at = overrides.pop("entry_at", NOW)
    exit_at = overrides.pop("exit_at", NOW + timedelta(hours=2))
    defaults: dict[str, Any] = {
        "lifecycle_signal_id": signal_id,
        "plan": plan,
        "entry_observation": observation(
            signal_id, plan, state="ENTRY_CONFIRMED", event_type="ENTRY_CONFIRMED", at=entry_at
        ),
        "exit_observation": observation(
            signal_id,
            plan,
            state="TARGET_1_REACHED",
            event_type="TARGET_1_REACHED",
            at=exit_at,
            state_version=5,
        ),
        "entry_market": market_reference(100.05, entry_at + timedelta(seconds=20)),
        "exit_market": market_reference(104.1, exit_at),
        "fee_schedule": FEE_SCHEDULE,
        "funding": funding_snapshot(entry_at),
        "funding_interval_hours": 8.0,
        "session_allowed": True,
        "session_state": "CRYPTO_24_7",
        "data_quality_ok": True,
        "bot_paused": False,
        "duplicate_exists": False,
        "plan_audit_available": True,
        "candidate_audit_available": True,
        "run_id": uuid.uuid4(),
        "as_of": entry_at,
        "exit_reason": SimulationExitReason.TARGET_1_OBSERVED,
        "metadata": {},
    }
    defaults.update(overrides)
    return SimulationInputs(**defaults)


# ------------------------------------------------------------- replay events


def _event(
    category: ReplayEventCategory,
    symbol: str,
    at: datetime,
    sequence: int,
    payload: dict[str, Any],
) -> ReplayEvent:
    return ReplayEvent(
        as_of=at, category=category.value, symbol=symbol, sequence=sequence, payload=payload
    )


def seeded_plan(
    *,
    symbol: str = "BTC-PERP",
    instrument_pk: int = 1,
    as_of: datetime | None = None,
    expiry_minutes: int = 240,
):
    """A REAL phase-9 ELIGIBLE plan built by the actual risk engine.

    Used as a replay input so the lifecycle/simulation stages can be
    exercised over historical market data without depending on synthetic
    candles satisfying every phase-8 gate at once.
    """
    import dataclasses

    from tests.risk_helpers import COST_SETTINGS as RISK_COSTS
    from tests.risk_helpers import RISK_SETTINGS as RISK_CFG
    from tests.risk_helpers import make_candidate, make_context

    from app.risk.risk_engine import evaluate_candidate

    context = make_context(candidate=make_candidate(instrument_pk=instrument_pk, symbol=symbol))
    outcome = evaluate_candidate(context, RISK_CFG, RISK_COSTS)
    assert outcome.plan is not None, "risk fixture must yield an eligible plan"
    plan = outcome.plan
    if as_of is not None:
        plan = dataclasses.replace(
            plan, as_of=as_of, expiry_at=as_of + timedelta(minutes=expiry_minutes)
        )
    return dataclasses.replace(plan, symbol=symbol, instrument_pk=instrument_pk)


def replay_scenario(
    *,
    symbol: str = "BTC-PERP",
    trend_up: bool = True,
    with_funding: bool = True,
    with_book: bool = True,
    book_depth: float = 500.0,
    candle_limit: int | None = None,
) -> tuple[list[ReplayEvent], datetime, datetime]:
    """Full synthetic replay: session, status, book, BBO, funding, candles.

    Candle events are stamped at their CLOSE time, so a candle only becomes
    observable once the replay clock passed its close - the same rule the
    live pipeline follows.
    """
    series, _ = default_series_set(trend_up=trend_up, settings=STRATEGY_SETTINGS)
    events: list[ReplayEvent] = []
    sequence = 0
    all_candles: list[tuple[str, Any]] = []
    for timeframe in ("1h", "15m", "5m"):
        for candle in series[timeframe].candles:
            all_candles.append((timeframe, candle))
    all_candles.sort(key=lambda item: item[1].open_time)
    start_at = all_candles[0][1].open_time
    minutes = {"5m": 5, "15m": 15, "1h": 60}

    emitted = 0
    for timeframe, candle in all_candles:
        close_at = candle.open_time + timedelta(minutes=minutes[timeframe])
        if candle_limit is not None and timeframe == "5m" and emitted >= candle_limit:
            continue
        if timeframe == "5m":
            emitted += 1
        sequence += 1
        if with_book:
            events.append(
                _event(
                    ReplayEventCategory.DATA_QUALITY_BOOK,
                    symbol,
                    close_at,
                    sequence,
                    {
                        "bids": [[candle.close - 0.05 * i, book_depth] for i in range(1, 11)],
                        "asks": [[candle.close + 0.05 * i, book_depth] for i in range(1, 11)],
                        "fresh": True,
                        "data_quality_status": "HEALTHY",
                        "market_quality_score": 90,
                    },
                )
            )
        sequence += 1
        events.append(
            _event(
                ReplayEventCategory.TICKER_BBO_TRADES,
                symbol,
                close_at,
                sequence,
                {
                    "kind": "bbo",
                    "best_bid": candle.close - 0.05,
                    "best_ask": candle.close + 0.05,
                    "mark_price": candle.close,
                },
            )
        )
        sequence += 1
        events.append(
            _event(
                ReplayEventCategory.CLOSED_CANDLE,
                symbol,
                close_at,
                sequence,
                {
                    "timeframe": timeframe,
                    "open_time": candle.open_time.isoformat(),
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "trade_count": candle.trade_count,
                },
            )
        )

    end_at = max(event.as_of for event in events)
    header = [
        _event(
            ReplayEventCategory.SESSION_CALENDAR,
            symbol,
            start_at,
            0,
            {"session_state": "CRYPTO_24_7", "session_allowed": True, "watchlist_active": True},
        ),
        _event(
            ReplayEventCategory.INSTRUMENT_STATUS,
            symbol,
            start_at,
            0,
            {
                "status": "ACTIVE",
                "instrument_pk": 1,
                "instrument_id": 1,
                "asset_class": "CRYPTO",
            },
        ),
    ]
    if with_funding:
        funding_at = start_at
        index = 0
        while funding_at <= end_at:
            index += 1
            header.append(
                _event(
                    ReplayEventCategory.TICKER_BBO_TRADES,
                    symbol,
                    funding_at,
                    900_000 + index,
                    {
                        "kind": "funding",
                        "funding_rate": 0.00001,
                        "interval_hours": 8.0,
                        "next_funding_at": (funding_at + timedelta(hours=8)).isoformat(),
                    },
                )
            )
            funding_at += timedelta(hours=1)
    return header + events, start_at, end_at


def lifecycle_replay_scenario(
    *,
    symbol: str = "BTC-PERP",
    start_at: datetime | None = None,
    path: list[float] | None = None,
    book_depth: float = 500.0,
    with_funding: bool = True,
    with_book: bool = True,
    candle_span_minutes: int = 5,
) -> tuple[list[ReplayEvent], datetime, datetime]:
    """Replay a 5m price path for a seeded plan's lifecycle.

    Default path walks into the reference entry zone [99.5, 99.6] and then
    up beyond reference target 1 (107) - artificial local test data.
    """
    start_at = start_at or NOW
    # holds near the reference zone for one candle so a DELAYED follower
    # entry is still inside the configured chase tolerance
    path = path or [100.4, 100.0, 99.55, 99.62, 100.9, 103.0, 105.0, 107.4]
    events: list[ReplayEvent] = [
        _event(
            ReplayEventCategory.SESSION_CALENDAR,
            symbol,
            start_at,
            0,
            {"session_state": "CRYPTO_24_7", "session_allowed": True, "watchlist_active": True},
        ),
        _event(
            ReplayEventCategory.INSTRUMENT_STATUS,
            symbol,
            start_at,
            0,
            {"status": "ACTIVE", "instrument_pk": 1, "instrument_id": 1, "asset_class": "CRYPTO"},
        ),
    ]
    if with_funding:
        events.append(
            _event(
                ReplayEventCategory.TICKER_BBO_TRADES,
                symbol,
                start_at,
                1,
                {
                    "kind": "funding",
                    "funding_rate": 0.00001,
                    "interval_hours": 8.0,
                    "next_funding_at": (start_at + timedelta(hours=8)).isoformat(),
                },
            )
        )
    sequence = 10
    open_time = start_at
    for close in path:
        close_at = open_time + timedelta(minutes=candle_span_minutes)
        if with_book:
            sequence += 1
            events.append(
                _event(
                    ReplayEventCategory.DATA_QUALITY_BOOK,
                    symbol,
                    close_at,
                    sequence,
                    {
                        "bids": [[close - 0.05 * i, book_depth] for i in range(1, 11)],
                        "asks": [[close + 0.05 * i, book_depth] for i in range(1, 11)],
                        "fresh": True,
                        "data_quality_status": "HEALTHY",
                        "market_quality_score": 90,
                    },
                )
            )
        sequence += 1
        events.append(
            _event(
                ReplayEventCategory.TICKER_BBO_TRADES,
                symbol,
                close_at,
                sequence,
                {
                    "kind": "bbo",
                    "best_bid": close - 0.05,
                    "best_ask": close + 0.05,
                    "mark_price": close,
                },
            )
        )
        sequence += 1
        events.append(
            _event(
                ReplayEventCategory.CLOSED_CANDLE,
                symbol,
                close_at,
                sequence,
                {
                    "timeframe": "5m",
                    "open_time": open_time.isoformat(),
                    "open": close + 0.1,
                    "high": close + 0.3,
                    "low": close - 0.3,
                    "close": close,
                    "volume": 500.0,
                    "trade_count": 40,
                },
            )
        )
        open_time = close_at
    return events, start_at, open_time
