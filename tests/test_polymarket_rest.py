"""Polymarket REST client: parsing, caching, pagination, retries. No real network."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from tests.conftest import FakeClock

from app.adapters.polymarket_rest import PolymarketRestClient, PolymarketRestError
from app.config import PolymarketSettings
from app.domain.enums import AssetClass, Timeframe


def build_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[PolymarketRestClient, FakeClock, list[float]]:
    settings = PolymarketSettings(_env_file=None)
    clock = FakeClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.rest_base_url
    )
    client = PolymarketRestClient(
        settings, client=http, clock=clock, sleep=fake_sleep, rng=lambda: 0.0
    )
    return client, clock, sleeps


async def test_get_instruments_parses_and_caches(instruments_payload: list[dict[str, Any]]) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        assert request.url.path == "/v1/info/instruments"
        return httpx.Response(200, json=instruments_payload)

    client, clock, _ = build_client(handler)
    instruments = await client.get_instruments()
    assert calls["count"] == 1
    assert [meta.symbol for meta in instruments] == ["BTC-PERP", "AAPL-PERP", "DOGE-PERP"]
    assert instruments[0].asset_class is AssetClass.CRYPTO
    assert instruments[1].asset_class is AssetClass.EQUITY
    assert instruments[0].max_leverage == 10

    # cached second call - no new request
    await client.get_instruments()
    assert calls["count"] == 1

    # TTL expiry -> refetch
    clock.advance(301)
    await client.get_instruments()
    assert calls["count"] == 2

    # bypass cache explicitly
    await client.get_instruments(use_cache=False)
    assert calls["count"] == 3
    await client.aclose()


async def test_get_klines_follows_more_flag() -> None:
    pages: list[dict[str, Any]] = [
        {
            "data": [
                [1_700_000_000_000, "100", "101", "99", "100.5", "10", 5],
                [1_700_000_060_000, "100.5", "102", "100", "101.5", "12", 6],
            ],
            "more": True,
        },
        {
            "data": [
                [1_700_000_120_000, "101.5", "103", "101", "102", "8", 4],
            ],
            "more": False,
        },
    ]
    seen_starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/info/klines"
        seen_starts.append(int(request.url.params["start_timestamp"]))
        return httpx.Response(200, json=pages[len(seen_starts) - 1])

    client, _, _ = build_client(handler)
    candles = await client.get_klines(1, Timeframe.M1, 1_700_000_000_000)
    assert len(candles) == 3
    assert [c.open_time for c in candles] == sorted(c.open_time for c in candles)
    # second page must start one interval after the last received candle
    assert seen_starts == [1_700_000_000_000, 1_700_000_060_000 + 60_000]
    await client.aclose()


async def test_retry_on_429_honours_retry_after() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=[])

    client, _, sleeps = build_client(handler)
    tickers = await client.get_tickers()
    assert tickers == []
    assert attempts["count"] == 2
    assert any(delay >= 3.0 for delay in sleeps)
    await client.aclose()


async def test_non_retryable_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client, _, _ = build_client(handler)
    with pytest.raises(PolymarketRestError) as excinfo:
        await client.get_book(999)
    assert excinfo.value.status_code == 404
    await client.aclose()


async def test_retries_are_bounded() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(503)

    client, _, _ = build_client(handler)
    with pytest.raises(PolymarketRestError):
        await client.get_tickers()
    # max_retries=3 -> exactly 4 attempts, never an endless loop
    assert attempts["count"] == 4
    await client.aclose()


async def test_orderbook_parsing_and_spread() -> None:
    payload = {
        "instrument_id": 1,
        "bids": [["100.00", "2"], ["99.50", "5"]],
        "asks": [["100.10", "1"], ["100.20", "4"]],
        "timestamp": 1_700_000_000_000,
        "sequence": 42,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["depth"] == "100"
        return httpx.Response(200, json=payload)

    client, _, _ = build_client(handler)
    book = await client.get_book(1)
    assert book.best_bid is not None and str(book.best_bid.price) == "100.00"
    assert book.best_ask is not None and str(book.best_ask.price) == "100.10"
    assert book.spread_bps is not None and float(book.spread_bps) == pytest.approx(9.995, rel=1e-3)
    await client.aclose()


async def test_funding_history_parsing() -> None:
    payload = {"data": [{"funding_rate": "0.0001", "timestamp": 1_700_000_000_000}], "more": False}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, _, _ = build_client(handler)
    rates = await client.get_funding_history(1)
    assert len(rates) == 1
    assert str(rates[0].funding_rate) == "0.0001"
    assert rates[0].ts.tzinfo is not None
    await client.aclose()


async def test_ticker_parsing() -> None:
    payload = [
        {
            "instrument_id": 1,
            "symbol": "BTC-PERP",
            "index_price": "65000.00",
            "mark_price": "65012.50",
            "last_price": "65010.00",
            "mid_price": "65011.25",
            "open_interest": "125.4",
            "funding_rate": "0.0001",
            "next_funding": 1_766_124_000_000,
            "timestamp": 1_766_120_400_000,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, _, _ = build_client(handler)
    tickers = await client.get_tickers(1)
    ticker = tickers[0]
    assert str(ticker.mark_price) == "65012.50"
    assert ticker.next_funding_at is not None and ticker.next_funding_at.tzinfo is not None
    assert ticker.ts.tzinfo is not None
    await client.aclose()
