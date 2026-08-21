"""Read-only REST client for the public Polymarket Perps market-data API.

Strictly public endpoints only - no authentication, no account data, no order
placement.  Features:

- weighted rate limiting against the documented per-IP token budget
- bounded retries with exponential backoff + jitter (no aggressive loops),
  honouring ``Retry-After`` on 429 responses
- in-memory TTL response caching for instruments and historical candles
- kline pagination via the API ``more`` continuation flag
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.config import PolymarketSettings
from app.domain.enums import Timeframe
from app.domain.models import (
    CandleData,
    FundingRateData,
    InstrumentMeta,
    OrderbookData,
    TickerData,
    TradeData,
)
from app.observability.logging import get_logger

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_KLINE_PAGES = 50


class PolymarketRestError(Exception):
    """Raised when the REST API returns a non-retryable error or retries are exhausted."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _TTLCache:
    """Small monotonic-clock TTL cache for REST responses."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        self._store[key] = (self._clock() + ttl_seconds, value)

    def clear(self) -> None:
        self._store.clear()


class PolymarketRestClient:
    def __init__(
        self,
        settings: PolymarketSettings,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        from app.adapters.rate_limiter import WeightedRateLimiter

        self._settings = settings
        self._weights = settings.endpoint_weights
        self._client = client or httpx.AsyncClient(
            base_url=settings.rest_base_url,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": "polysignal-intelligence/0.1 (read-only market data)"},
        )
        self._owns_client = client is None
        self._limiter = limiter or WeightedRateLimiter(
            settings.rate_limit_budget_per_minute, clock=clock, sleep=sleep
        )
        self._cache = _TTLCache(clock=clock)
        self._sleep = sleep
        self._rng = rng
        self._log = get_logger("polymarket_rest")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ core

    async def _request(self, path: str, params: dict[str, Any] | None, weight: int) -> Any:
        last_error: str = "unknown error"
        last_status: int | None = None
        for attempt in range(self._settings.max_retries + 1):
            await self._limiter.acquire(weight)
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc.__class__.__name__}"
                last_status = None
                self._log.warning(
                    "rest_transport_error", path=path, attempt=attempt, error=str(exc)
                )
                await self._backoff(attempt, retry_after=None)
                continue

            if response.status_code == 200:
                return response.json()

            last_status = response.status_code
            last_error = f"HTTP {response.status_code}"
            if response.status_code in _RETRYABLE_STATUS and attempt < self._settings.max_retries:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                self._log.warning(
                    "rest_retryable_status",
                    path=path,
                    status=response.status_code,
                    attempt=attempt,
                    retry_after=retry_after,
                )
                await self._backoff(attempt, retry_after=retry_after)
                continue
            raise PolymarketRestError(
                f"GET {path} failed: {last_error}", status_code=response.status_code
            )
        raise PolymarketRestError(
            f"GET {path} failed after {self._settings.max_retries + 1} attempts: {last_error}",
            status_code=last_status,
        )

    async def _backoff(self, attempt: int, retry_after: float | None) -> None:
        base = self._settings.backoff_base_seconds * (2**attempt)
        jitter = base * 0.25 * self._rng()
        delay = min(base + jitter, self._settings.backoff_max_seconds)
        if retry_after is not None:
            delay = min(max(delay, retry_after), self._settings.backoff_max_seconds)
        await self._sleep(delay)

    # ------------------------------------------------------------- endpoints

    async def get_instruments(self, *, use_cache: bool = True) -> list[InstrumentMeta]:
        cache_key = "instruments"
        if use_cache and (cached := self._cache.get(cache_key)) is not None:
            return cached  # type: ignore[no-any-return]
        payload = await self._request("/v1/info/instruments", None, self._weights.instruments)
        instruments = [InstrumentMeta.from_api(item) for item in payload]
        self._cache.set(cache_key, instruments, self._settings.instrument_cache_ttl_seconds)
        return instruments

    async def get_tickers(self, instrument_id: int | None = None) -> list[TickerData]:
        params = {"instrument_id": instrument_id} if instrument_id is not None else None
        weight = (
            self._weights.ticker_single if instrument_id is not None else self._weights.tickers_all
        )
        payload = await self._request("/v1/info/tickers", params, weight)
        return [TickerData.from_api(item) for item in payload]

    async def get_book(self, instrument_id: int, depth: int = 100) -> OrderbookData:
        payload = await self._request(
            "/v1/info/book",
            {"instrument_id": instrument_id, "depth": depth},
            self._weights.for_book_depth(depth),
        )
        return OrderbookData.from_api(payload)

    async def get_klines(
        self,
        instrument_id: int,
        timeframe: Timeframe,
        start_timestamp_ms: int,
        end_timestamp_ms: int | None = None,
        *,
        use_cache: bool = True,
    ) -> list[CandleData]:
        """Fetch candles, following the ``more`` continuation flag."""
        cache_key = (
            f"klines:{instrument_id}:{timeframe.value}:{start_timestamp_ms}:{end_timestamp_ms}"
        )
        if use_cache and (cached := self._cache.get(cache_key)) is not None:
            return cached  # type: ignore[no-any-return]

        candles: list[CandleData] = []
        cursor = start_timestamp_ms
        for _page in range(_MAX_KLINE_PAGES):
            params: dict[str, Any] = {
                "instrument_id": instrument_id,
                "interval": timeframe.value,
                "start_timestamp": cursor,
            }
            if end_timestamp_ms is not None:
                params["end_timestamp"] = end_timestamp_ms
            payload = await self._request("/v1/info/klines", params, self._weights.klines)
            rows = payload.get("data", [])
            candles.extend(CandleData.from_api_row(row, timeframe) for row in rows)
            if not payload.get("more") or not rows:
                break
            cursor = int(rows[-1][0]) + timeframe.milliseconds
        else:
            self._log.warning(
                "klines_pagination_capped",
                instrument_id=instrument_id,
                timeframe=timeframe.value,
                pages=_MAX_KLINE_PAGES,
            )

        # De-duplicate on open_time (pagination overlap safety) and sort.
        unique = {candle.open_time: candle for candle in candles}
        result = sorted(unique.values(), key=lambda c: c.open_time)
        self._cache.set(cache_key, result, self._settings.klines_cache_ttl_seconds)
        return result

    async def get_trades(
        self,
        instrument_id: int,
        start_timestamp_ms: int | None = None,
        end_timestamp_ms: int | None = None,
    ) -> list[TradeData]:
        params: dict[str, Any] = {"instrument_id": instrument_id}
        if start_timestamp_ms is not None:
            params["start_timestamp"] = start_timestamp_ms
        if end_timestamp_ms is not None:
            params["end_timestamp"] = end_timestamp_ms
        payload = await self._request("/v1/info/trades", params, self._weights.trades)
        return [TradeData.from_api(item) for item in payload.get("data", [])]

    async def get_funding_history(
        self,
        instrument_id: int,
        start_timestamp_ms: int | None = None,
        end_timestamp_ms: int | None = None,
    ) -> list[FundingRateData]:
        params: dict[str, Any] = {"instrument_id": instrument_id}
        if start_timestamp_ms is not None:
            params["start_timestamp"] = start_timestamp_ms
        if end_timestamp_ms is not None:
            params["end_timestamp"] = end_timestamp_ms
        payload = await self._request("/v1/info/funding", params, self._weights.funding)
        return [FundingRateData.from_api(item) for item in payload.get("data", [])]


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
