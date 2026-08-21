"""Event validation: invalid payloads must be rejected with structured reasons."""

from __future__ import annotations

from decimal import Decimal

from app.data import event_validation as ev


def _ticker(**overrides):
    payload = {
        "instrument_id": 1,
        "symbol": "BTC-PERP",
        "mark_price": "65012.50",
        "index_price": "65000.00",
        "last_price": "65010.00",
        "timestamp": 1766120400000,
    }
    payload.update(overrides)
    return payload


def test_valid_ticker_accepted() -> None:
    outcome = ev.validate_ticker(_ticker())
    assert outcome.ok
    assert outcome.parsed.mark_price == Decimal("65012.50")


def test_ticker_negative_price_rejected() -> None:
    outcome = ev.validate_ticker(_ticker(mark_price="-1"))
    assert not outcome.ok and outcome.reason == ev.NON_POSITIVE_PRICE


def test_ticker_nan_and_infinity_rejected() -> None:
    assert ev.validate_ticker(_ticker(mark_price="NaN")).reason == ev.NON_FINITE_VALUE
    assert ev.validate_ticker(_ticker(index_price="Infinity")).reason == ev.NON_FINITE_VALUE


def test_ticker_bad_timestamp_rejected() -> None:
    assert ev.validate_ticker(_ticker(timestamp=None)).reason == ev.BAD_TIMESTAMP
    assert ev.validate_ticker(_ticker(timestamp=123)).reason == ev.BAD_TIMESTAMP  # seconds, not ms


def test_ticker_missing_instrument_rejected() -> None:
    payload = _ticker()
    del payload["instrument_id"]
    assert ev.validate_ticker(payload).reason == ev.MISSING_FIELD


def test_ticker_outlier_rejected() -> None:
    outcome = ev.validate_ticker(
        _ticker(mark_price="72000"),
        reference_price=Decimal("65000"),
        max_deviation_bps=500.0,
    )
    assert not outcome.ok and outcome.reason == ev.OUTLIER_PRICE
    # within bounds passes
    assert ev.validate_ticker(
        _ticker(mark_price="65100"),
        reference_price=Decimal("65000"),
        max_deviation_bps=500.0,
    ).ok


def test_ticker_malformed_payload_rejected() -> None:
    assert ev.validate_ticker(["not", "a", "dict"]).reason == ev.MALFORMED


def test_valid_bbo_accepted_both_shapes() -> None:
    nested = {
        "instrument_id": 1,
        "bid": ["100.0", "2"],
        "ask": ["100.1", "1"],
        "timestamp": 1766120400000,
    }
    flat = {
        "instrument_id": 1,
        "bid_price": "100.0",
        "bid_quantity": "2",
        "ask_price": "100.1",
        "ask_quantity": "1",
        "timestamp": 1766120400000,
    }
    for payload in (nested, flat):
        outcome = ev.validate_bbo(payload)
        assert outcome.ok
        assert outcome.parsed.spread_bps is not None


def test_crossed_bbo_rejected() -> None:
    payload = {
        "instrument_id": 1,
        "bid": ["100.2", "1"],
        "ask": ["100.1", "1"],
        "timestamp": 1766120400000,
    }
    assert ev.validate_bbo(payload).reason == ev.CROSSED_BBO


def test_bbo_zero_price_rejected() -> None:
    payload = {
        "instrument_id": 1,
        "bid": ["0", "1"],
        "ask": ["100.1", "1"],
        "timestamp": 1766120400000,
    }
    assert ev.validate_bbo(payload).reason == ev.NON_POSITIVE_PRICE


def test_bbo_negative_quantity_rejected() -> None:
    payload = {
        "instrument_id": 1,
        "bid": ["100.0", "-1"],
        "ask": ["100.1", "1"],
        "timestamp": 1766120400000,
    }
    assert ev.validate_bbo(payload).reason == ev.NEGATIVE_QUANTITY


def test_trade_validation() -> None:
    valid = {
        "trade_id": 1,
        "instrument_id": 1,
        "side": "long",
        "price": "100.0",
        "quantity": "0.5",
        "timestamp": 1766120400000,
    }
    assert ev.validate_trade(valid).ok
    assert ev.validate_trade({**valid, "quantity": "0"}).reason == ev.NEGATIVE_QUANTITY
    assert ev.validate_trade({**valid, "price": "nan"}).reason == ev.NON_FINITE_VALUE
    assert (
        ev.validate_trade(
            {**valid, "price": "200.0"},
            reference_price=Decimal("100"),
            max_deviation_bps=500.0,
        ).reason
        == ev.OUTLIER_PRICE
    )


def test_kline_validation() -> None:
    row = [1766120400000, "100", "101", "99", "100.5", "10", 5]
    assert ev.validate_kline(row, "1m").ok
    assert ev.validate_kline(row, "2m").reason == ev.INVALID_TIMEFRAME
    assert ev.validate_kline([1766120400000, "100"], "1m").reason == ev.MALFORMED
    # high < low is inconsistent
    bad = [1766120400000, "100", "98", "99", "100", "10", 5]
    assert ev.validate_kline(bad, "1m").reason == ev.MALFORMED
    negative_volume = [1766120400000, "100", "101", "99", "100", "-1", 5]
    assert ev.validate_kline(negative_volume, "1m").reason == ev.NEGATIVE_QUANTITY


def test_kline_object_shape_accepted() -> None:
    payload = {
        "timestamp": 1766120400000,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "volume": "10",
        "trades": 5,
    }
    assert ev.validate_kline(payload, "5m").ok


def test_book_message_validation() -> None:
    snapshot = {
        "instrument_id": 1,
        "type": "snapshot",
        "bids": [["100.0", "1"], ["99.5", "2"]],
        "asks": [["100.1", "1"]],
        "timestamp": 1766120400000,
        "sequence": 10,
    }
    outcome = ev.validate_book_message(snapshot)
    assert outcome.ok and outcome.parsed.is_snapshot

    crossed = {**snapshot, "bids": [["100.2", "1"]]}
    assert ev.validate_book_message(crossed).reason == ev.CROSSED_BOOK

    negative = {**snapshot, "asks": [["100.1", "-3"]]}
    assert ev.validate_book_message(negative).reason == ev.NEGATIVE_QUANTITY

    bad_sequence = {**snapshot, "sequence": "abc"}
    assert ev.validate_book_message(bad_sequence).reason == ev.BAD_SEQUENCE

    # delta with zero quantity (level removal) is fine
    delta = {
        "instrument_id": 1,
        "bids": [["100.0", "0"]],
        "asks": [],
        "timestamp": 1766120401000,
        "sequence": 11,
    }
    outcome = ev.validate_book_message(delta)
    assert outcome.ok and not outcome.parsed.is_snapshot
