"""Real-time market data orchestration.

Consumes frames from the central WebSocket client, validates every event,
updates the in-memory order books, the Redis market cache and the persistence
buffers, and tracks per-instrument/channel health counters for the
DataQualityService.

This module contains NO strategy, risk, cost, signal or Telegram logic - it
only provides trustworthy data for later phases.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from app.adapters.polymarket_ws import PolymarketWsClient, WsFrame
from app.config import DataQualitySettings, PolymarketWsSettings
from app.data import event_validation as ev
from app.data.market_cache import MarketCache
from app.data.orderbook_service import OrderbookManager
from app.data.persistence_buffer import PersistenceBuffer
from app.domain.enums import (
    Channel,
    DataSource,
    FreshnessStatus,
    SystemEventLevel,
    WsConnectionState,
)
from app.domain.models import CandleData, FundingRateData, TickerData, TradeData, ms_to_utc
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.system_event_repository import SystemEventRepository

_CHANNEL_MAP = {
    "tickers": Channel.TICKER,
    "bbo": Channel.BBO,
    "book": Channel.ORDERBOOK,
    "trades": Channel.TRADES,
    "klines": Channel.KLINES,
}

#: minimum spacing between order book resync requests per instrument
_RESYNC_MIN_INTERVAL_SECONDS = 5.0
#: at most one persisted tick per instrument per bucket
_TICK_PERSIST_BUCKET_MS = 5_000
#: at most one persisted order book snapshot per instrument per bucket
_BOOK_PERSIST_BUCKET_MS = 60_000
#: order book levels persisted per side
_BOOK_PERSIST_LEVELS = 20


@dataclass
class ChannelHealth:
    last_event_ts: datetime | None = None
    last_local_at: datetime | None = None
    valid_count: int = 0
    invalid_count: int = 0
    last_invalid_reason: str | None = None
    invalid_times: deque[float] = field(default_factory=deque)


@dataclass
class InstrumentTracker:
    instrument_pk: int
    instrument_id: int
    symbol: str
    channels: dict[Channel, ChannelHealth] = field(default_factory=dict)
    last_mark_price: Decimal | None = None
    last_index_price: Decimal | None = None
    last_funding_rate: Decimal | None = None
    last_resync_request_at: float | None = None
    resync_requests: int = 0

    def channel(self, channel: Channel) -> ChannelHealth:
        if channel not in self.channels:
            self.channels[channel] = ChannelHealth()
        return self.channels[channel]

    def invalid_in_window(self, now_monotonic: float, window_seconds: float) -> int:
        total = 0
        for health in self.channels.values():
            times = health.invalid_times
            while times and now_monotonic - times[0] > window_seconds:
                times.popleft()
            total += len(times)
        return total


@dataclass
class PersistenceBundle:
    """All persistence buffers used by the market data service."""

    ticks: PersistenceBuffer[tuple[int, TickerData]]
    candles: PersistenceBuffer[tuple[int, CandleData]]
    books: PersistenceBuffer[dict[str, Any]]
    funding: PersistenceBuffer[tuple[int, FundingRateData]]

    def all(self) -> list[PersistenceBuffer[Any]]:
        return [self.ticks, self.candles, self.books, self.funding]

    @property
    def degraded(self) -> bool:
        return any(buffer.degraded for buffer in self.all())

    async def start(self) -> None:
        for buffer in self.all():
            await buffer.start()

    async def stop(self) -> None:
        for buffer in self.all():
            await buffer.stop()

    def stats(self) -> dict[str, dict[str, float | int | bool]]:
        return {buffer.name: buffer.stats() for buffer in self.all()}


class MarketDataService:
    def __init__(
        self,
        ws_client: PolymarketWsClient,
        cache: MarketCache,
        books: OrderbookManager,
        buffers: PersistenceBundle | None,
        ws_settings: PolymarketWsSettings,
        quality_settings: DataQualitySettings,
        system_events: SystemEventRepository | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._ws = ws_client
        self._cache = cache
        self._books = books
        self._buffers = buffers
        self._ws_settings = ws_settings
        self._quality = quality_settings
        self._system_events = system_events
        self._clock = clock
        self._now = now_fn
        self._log = get_logger("market_data_service")

        self._trackers_by_id: dict[int, InstrumentTracker] = {}
        self._consume_task: asyncio.Task[None] | None = None

        self.unknown_instrument_frames = 0
        self.unknown_channel_frames = 0

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._buffers is not None:
            await self._buffers.start()
        if self._consume_task is None:
            self._consume_task = asyncio.create_task(self._consume(), name="market_data_consume")

    async def stop(self) -> None:
        if self._consume_task is not None:
            self._consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consume_task
            self._consume_task = None
        if self._buffers is not None:
            await self._buffers.stop()

    # -------------------------------------------------------------- universe

    async def sync_universe(self, instruments: list[tuple[int, int, str]]) -> None:
        """Reconcile subscriptions with the enabled universe.

        ``instruments`` are (instrument_pk, exchange instrument_id, symbol)
        triples of ENABLED instruments only.  Removed/delisted instruments are
        unsubscribed idempotently and their in-memory state is dropped.
        """
        new_ids = {instrument_id for _, instrument_id, _ in instruments}
        removed = set(self._trackers_by_id) - new_ids
        for instrument_id in removed:
            tracker = self._trackers_by_id.pop(instrument_id)
            self._books.drop(instrument_id)
            self._log.info(
                "instrument_deactivated", symbol=tracker.symbol, instrument_id=instrument_id
            )
        for instrument_pk, instrument_id, symbol in instruments:
            if instrument_id not in self._trackers_by_id:
                self._trackers_by_id[instrument_id] = InstrumentTracker(
                    instrument_pk=instrument_pk, instrument_id=instrument_id, symbol=symbol
                )

        channels: set[str] = set()
        for _, instrument_id, _ in instruments:
            channels.add(f"tickers::{instrument_id}")
            channels.add(f"bbo::{instrument_id}")
            channels.add(f"book::{instrument_id}")
            channels.add(f"trades::{instrument_id}")
            for timeframe in self._ws_settings.kline_timeframes:
                channels.add(f"klines::{instrument_id}::{timeframe}")
        await self._ws.set_subscriptions(channels)
        metrics.set_gauge("market_data.active_instruments", float(len(self._trackers_by_id)))

    # ------------------------------------------------------------- ws state

    async def on_ws_state(self, state: WsConnectionState) -> None:
        await self._cache.set_ws_state(state)
        await self._emit_system_event(
            SystemEventLevel.WARNING
            if state in (WsConnectionState.RECONNECTING, WsConnectionState.DEGRADED)
            else SystemEventLevel.INFO,
            "ws_state_change",
            f"websocket state -> {state.value}",
            {"state": state.value},
        )

    # -------------------------------------------------------------- consume

    async def _consume(self) -> None:
        while True:
            try:
                frame = await asyncio.wait_for(self._ws.queue.get(), timeout=5.0)
            except TimeoutError:
                await self._cache.heartbeat("market_data_service", self._now())
                continue
            try:
                await self._handle_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # a single bad frame must never kill the pipeline
                self._log.error(
                    "frame_handling_failed", channel=frame.channel, error=type(exc).__name__
                )
                metrics.increment("market_data.handler_errors")

    async def _handle_frame(self, frame: WsFrame) -> None:
        parts = frame.channel.split("::")
        channel = _CHANNEL_MAP.get(parts[0])
        if channel is None:
            self.unknown_channel_frames += 1
            return
        if len(parts) < 2:
            self.unknown_channel_frames += 1
            return
        try:
            instrument_id = int(parts[1])
        except ValueError:
            self.unknown_channel_frames += 1
            return
        tracker = self._trackers_by_id.get(instrument_id)
        if tracker is None:
            self.unknown_instrument_frames += 1
            metrics.increment("market_data.unknown_instrument_frames")
            return

        if channel is Channel.TICKER:
            await self._handle_ticker(tracker, frame)
        elif channel is Channel.BBO:
            await self._handle_bbo(tracker, frame)
        elif channel is Channel.TRADES:
            await self._handle_trades(tracker, frame)
        elif channel is Channel.ORDERBOOK:
            await self._handle_book(tracker, frame)
        elif channel is Channel.KLINES:
            timeframe = parts[2] if len(parts) > 2 else ""
            await self._handle_kline(tracker, frame, timeframe)

    # ------------------------------------------------------------- handlers

    async def _handle_ticker(self, tracker: InstrumentTracker, frame: WsFrame) -> None:
        payload = dict(frame.data) if isinstance(frame.data, dict) else frame.data
        if isinstance(payload, dict):
            payload.setdefault("instrument_id", tracker.instrument_id)
            if frame.ts_ms is not None:
                payload.setdefault("timestamp", frame.ts_ms)
        outcome = ev.validate_ticker(
            payload,
            reference_price=tracker.last_mark_price,
            max_deviation_bps=self._quality.outlier_max_deviation_bps,
        )
        if not outcome.ok:
            self._record_invalid(tracker, Channel.TICKER, outcome.reason)
            return
        ticker = cast(TickerData, outcome.parsed)
        health = tracker.channel(Channel.TICKER)
        if health.last_event_ts is not None and ticker.ts < health.last_event_ts:
            self._record_invalid(tracker, Channel.TICKER, ev.OUT_OF_ORDER)
            return

        self._record_valid(tracker, Channel.TICKER, ticker.ts)
        if ticker.mark_price is not None:
            tracker.last_mark_price = ticker.mark_price
        if ticker.index_price is not None:
            tracker.last_index_price = ticker.index_price

        await self._cache.set_ticker(
            tracker.instrument_id,
            {
                "symbol": tracker.symbol,
                "mark_price": _s(ticker.mark_price),
                "index_price": _s(ticker.index_price),
                "last_price": _s(ticker.last_price),
                "mid_price": _s(ticker.mid_price),
                "open_interest": _s(ticker.open_interest),
                "funding_rate": _s(ticker.funding_rate),
                "ts": ticker.ts.isoformat(),
            },
        )
        await self._cache.set_last_update(
            DataSource.WEBSOCKET, Channel.TICKER.value, tracker.symbol, ticker.ts
        )

        if self._buffers is not None:
            self._buffers.ticks.append((tracker.instrument_pk, ticker))
            if ticker.funding_rate is not None and ticker.funding_rate != tracker.last_funding_rate:
                tracker.last_funding_rate = ticker.funding_rate
                self._buffers.funding.append(
                    (
                        tracker.instrument_pk,
                        FundingRateData(funding_rate=ticker.funding_rate, ts=ticker.ts),
                    )
                )

    async def _handle_bbo(self, tracker: InstrumentTracker, frame: WsFrame) -> None:
        payload = dict(frame.data) if isinstance(frame.data, dict) else frame.data
        if isinstance(payload, dict):
            payload.setdefault("instrument_id", tracker.instrument_id)
            if frame.ts_ms is not None:
                payload.setdefault("timestamp", frame.ts_ms)
        outcome = ev.validate_bbo(payload)
        if not outcome.ok:
            self._record_invalid(tracker, Channel.BBO, outcome.reason)
            return
        bbo = cast(ev.BboUpdate, outcome.parsed)
        ts = ms_to_utc(bbo.ts_ms)
        health = tracker.channel(Channel.BBO)
        if health.last_event_ts is not None and ts < health.last_event_ts:
            self._record_invalid(tracker, Channel.BBO, ev.OUT_OF_ORDER)
            return
        self._record_valid(tracker, Channel.BBO, ts)
        await self._cache.set_bbo(
            tracker.instrument_id,
            {
                "symbol": tracker.symbol,
                "bid_price": str(bbo.bid_price),
                "bid_quantity": str(bbo.bid_quantity),
                "ask_price": str(bbo.ask_price),
                "ask_quantity": str(bbo.ask_quantity),
                "spread_bps": _s(bbo.spread_bps),
                "ts": ts.isoformat(),
            },
        )
        await self._cache.set_last_update(
            DataSource.WEBSOCKET, Channel.BBO.value, tracker.symbol, ts
        )

    async def _handle_trades(self, tracker: InstrumentTracker, frame: WsFrame) -> None:
        entries = frame.data if isinstance(frame.data, list) else [frame.data]
        latest_ts: datetime | None = None
        for entry in entries:
            payload = dict(entry) if isinstance(entry, dict) else entry
            if isinstance(payload, dict):
                payload.setdefault("instrument_id", tracker.instrument_id)
                if frame.ts_ms is not None:
                    payload.setdefault("timestamp", frame.ts_ms)
            outcome = ev.validate_trade(
                payload,
                reference_price=tracker.last_mark_price,
                max_deviation_bps=self._quality.outlier_max_deviation_bps,
            )
            if not outcome.ok:
                self._record_invalid(tracker, Channel.TRADES, outcome.reason)
                continue
            trade = cast(TradeData, outcome.parsed)
            latest_ts = trade.ts if latest_ts is None else max(latest_ts, trade.ts)
        if latest_ts is not None:
            self._record_valid(tracker, Channel.TRADES, latest_ts)
            await self._cache.set_last_update(
                DataSource.WEBSOCKET, Channel.TRADES.value, tracker.symbol, latest_ts
            )

    async def _handle_kline(
        self, tracker: InstrumentTracker, frame: WsFrame, timeframe: str
    ) -> None:
        outcome = ev.validate_kline(frame.data, timeframe)
        if not outcome.ok:
            self._record_invalid(tracker, Channel.KLINES, outcome.reason)
            return
        candle = cast(CandleData, outcome.parsed)
        health = tracker.channel(Channel.KLINES)
        if health.last_event_ts is not None and candle.open_time < health.last_event_ts:
            # older bucket than the last seen one: reject as out of order
            self._record_invalid(tracker, Channel.KLINES, ev.OUT_OF_ORDER)
            return
        self._record_valid(tracker, Channel.KLINES, candle.open_time)
        await self._cache.set_last_update(
            DataSource.WEBSOCKET,
            f"{Channel.KLINES.value}:{candle.timeframe.value}",
            tracker.symbol,
            candle.open_time,
        )
        if self._buffers is not None:
            self._buffers.candles.append((tracker.instrument_pk, candle))

    async def _handle_book(self, tracker: InstrumentTracker, frame: WsFrame) -> None:
        payload = dict(frame.data) if isinstance(frame.data, dict) else frame.data
        if isinstance(payload, dict):
            if frame.ts_ms is not None:
                payload.setdefault("timestamp", frame.ts_ms)
            if frame.sequence is not None:
                payload.setdefault("sequence", frame.sequence)
        outcome = ev.validate_book_message(payload, instrument_id=tracker.instrument_id)
        if not outcome.ok:
            self._record_invalid(tracker, Channel.ORDERBOOK, outcome.reason)
            return
        message = cast(ev.BookMessage, outcome.parsed)
        reliable = self._books.apply(message)
        book = self._books.book(tracker.instrument_id)
        if not reliable or book.needs_resync:
            self._record_invalid(tracker, Channel.ORDERBOOK, ev.BAD_SEQUENCE)
            await self._request_book_resync(tracker)
            return

        ts = ms_to_utc(message.ts_ms)
        self._record_valid(tracker, Channel.ORDERBOOK, ts)
        await self._cache.set_orderbook_status(tracker.instrument_id, FreshnessStatus.FRESH)
        await self._cache.set_last_update(
            DataSource.WEBSOCKET, Channel.ORDERBOOK.value, tracker.symbol, ts
        )

        if self._buffers is not None and message.is_snapshot:
            top = book.top()
            self._buffers.books.append(
                {
                    "instrument_pk": tracker.instrument_pk,
                    "ts": ts,
                    "sequence": message.sequence,
                    "depth_level": self._ws_settings.orderbook_depth,
                    "bids": {
                        "levels": [
                            [str(level.price), str(level.quantity)]
                            for level in book.levels("bid", _BOOK_PERSIST_LEVELS)
                        ]
                    },
                    "asks": {
                        "levels": [
                            [str(level.price), str(level.quantity)]
                            for level in book.levels("ask", _BOOK_PERSIST_LEVELS)
                        ]
                    },
                    "best_bid": top.best_bid.price if top.best_bid else None,
                    "best_ask": top.best_ask.price if top.best_ask else None,
                    "spread_bps": top.spread_bps,
                    "source": DataSource.WEBSOCKET.value,
                }
            )

    async def _request_book_resync(self, tracker: InstrumentTracker) -> None:
        now = self._clock()
        if (
            tracker.last_resync_request_at is not None
            and now - tracker.last_resync_request_at < _RESYNC_MIN_INTERVAL_SECONDS
        ):
            return
        tracker.last_resync_request_at = now
        tracker.resync_requests += 1
        metrics.increment("market_data.book_resyncs")
        book = self._books.book(tracker.instrument_id)
        book.reset_for_resync()
        await self._cache.set_orderbook_status(tracker.instrument_id, FreshnessStatus.RESYNCING)
        await self._emit_system_event(
            SystemEventLevel.WARNING,
            "orderbook_resync",
            f"order book resync requested for {tracker.symbol}",
            {"symbol": tracker.symbol, "resync_requests": tracker.resync_requests},
        )
        await self._ws.resubscribe({f"book::{tracker.instrument_id}"})

    # ------------------------------------------------------------- tracking

    def _record_valid(
        self, tracker: InstrumentTracker, channel: Channel, event_ts: datetime
    ) -> None:
        health = tracker.channel(channel)
        health.valid_count += 1
        health.last_event_ts = event_ts
        health.last_local_at = self._now()

    def _record_invalid(
        self, tracker: InstrumentTracker, channel: Channel, reason: str | None
    ) -> None:
        health = tracker.channel(channel)
        health.invalid_count += 1
        health.last_invalid_reason = reason
        health.invalid_times.append(self._clock())
        metrics.increment("market_data.invalid_events")
        metrics.increment(f"market_data.invalid.{(reason or 'UNKNOWN').lower()}")
        self._log.warning(
            "invalid_event_rejected",
            symbol=tracker.symbol,
            channel=channel.value,
            reason=reason,
        )

    # ---------------------------------------------------------- introspection

    def trackers(self) -> list[InstrumentTracker]:
        return list(self._trackers_by_id.values())

    def tracker_for(self, instrument_id: int) -> InstrumentTracker | None:
        return self._trackers_by_id.get(instrument_id)

    @property
    def buffers(self) -> PersistenceBundle | None:
        return self._buffers

    def total_invalid_events(self) -> int:
        return sum(
            health.invalid_count
            for tracker in self._trackers_by_id.values()
            for health in tracker.channels.values()
        )

    def total_resync_requests(self) -> int:
        return sum(tracker.resync_requests for tracker in self._trackers_by_id.values())

    async def _emit_system_event(
        self,
        level: SystemEventLevel,
        event_type: str,
        message: str,
        context: dict[str, Any],
    ) -> None:
        if self._system_events is None:
            return
        try:
            await self._system_events.add(level, event_type, message, context)
        except Exception as exc:
            # persistence being down must not break the feed
            self._log.warning("system_event_persist_failed", error=type(exc).__name__)


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
