"""Domain model parsing and derived values."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.enums import AssetClass, SignalStatus, Timeframe
from app.domain.lifecycle import (
    ALLOWED_TRANSITIONS,
    OPEN_STATES,
    TERMINAL_STATES,
    is_transition_allowed,
)
from app.domain.models import BookLevel, CandleData, InstrumentMeta, OrderbookData, ms_to_utc


def test_ms_to_utc_is_timezone_aware() -> None:
    ts = ms_to_utc(1_700_000_000_000)
    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 0


def test_timeframe_milliseconds() -> None:
    assert Timeframe.M1.milliseconds == 60_000
    assert Timeframe.M5.milliseconds == 300_000
    assert Timeframe.M15.milliseconds == 900_000
    assert Timeframe.H1.milliseconds == 3_600_000


def test_instrument_asset_class_classification(instruments_payload) -> None:
    metas = [InstrumentMeta.from_api(item) for item in instruments_payload]
    assert metas[0].asset_class is AssetClass.CRYPTO
    assert metas[1].asset_class is AssetClass.EQUITY
    other = InstrumentMeta.from_api({"instrument_id": 9, "symbol": "X-PERP", "category": "fx"})
    assert other.asset_class is AssetClass.OTHER


def test_instrument_decimal_fields(instruments_payload) -> None:
    meta = InstrumentMeta.from_api(instruments_payload[0])
    assert meta.min_notional == Decimal("1")
    assert meta.liquidation_fee == Decimal("0.01")
    assert meta.risk_tiers[0].max_leverage == 10


def test_candle_from_api_row() -> None:
    candle = CandleData.from_api_row(
        [1_700_000_000_000, "100", "101.5", "99.5", "101", "42.5", 18], Timeframe.M5
    )
    assert candle.open == Decimal("100")
    assert candle.trade_count == 18
    assert candle.timeframe is Timeframe.M5


def test_orderbook_spread_bps() -> None:
    book = OrderbookData(
        instrument_id=1,
        bids=(BookLevel(price=Decimal("99.95"), quantity=Decimal("1")),),
        asks=(BookLevel(price=Decimal("100.05"), quantity=Decimal("1")),),
        ts=ms_to_utc(1_700_000_000_000),
    )
    assert book.mid_price == Decimal("100.00")
    assert book.spread_bps == pytest.approx(Decimal("10"))


def test_empty_orderbook_has_no_spread() -> None:
    book = OrderbookData(instrument_id=1, bids=(), asks=(), ts=ms_to_utc(0))
    assert book.best_bid is None
    assert book.spread_bps is None


def test_lifecycle_covers_all_states() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(SignalStatus)


def test_lifecycle_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        assert not ALLOWED_TRANSITIONS[state]
    assert SignalStatus.CLOSED_TP in TERMINAL_STATES
    assert SignalStatus.REJECTED in TERMINAL_STATES


def test_lifecycle_transition_checks() -> None:
    assert is_transition_allowed(SignalStatus.CANDIDATE, SignalStatus.WATCHING)
    assert is_transition_allowed(SignalStatus.WATCHING, SignalStatus.OPEN)
    assert not is_transition_allowed(SignalStatus.OPEN, SignalStatus.WATCHING)
    assert not is_transition_allowed(SignalStatus.CLOSED_STOP, SignalStatus.OPEN)
    assert SignalStatus.WATCHING in OPEN_STATES
