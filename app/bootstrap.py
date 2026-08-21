"""Application wiring: builds and tears down the process-wide AppContext."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.polymarket_rest import PolymarketRestClient
from app.adapters.polymarket_ws import PolymarketWsClient
from app.config import Settings
from app.data.data_quality import DataQualityService
from app.data.instrument_service import InstrumentService, UniverseRefreshResult
from app.data.market_cache import MarketCache
from app.data.market_data_service import MarketDataService, PersistenceBundle
from app.data.orderbook_service import OrderbookManager
from app.data.persistence_buffer import PersistenceBuffer
from app.domain.enums import DataSource
from app.domain.models import CandleData, FundingRateData, TickerData
from app.jobs.universe_refresh import UniverseRefreshJob
from app.observability.logging import configure_logging, get_logger
from app.repositories.candle_repository import CandleRepository
from app.repositories.database import Database
from app.repositories.decision_repository import DecisionRepository
from app.repositories.funding_repository import FundingRateRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.market_tick_repository import MarketTickRepository
from app.repositories.orderbook_repository import OrderbookSnapshotRepository
from app.repositories.signal_repository import SignalRepository
from app.repositories.system_event_repository import SystemEventRepository


@dataclass
class AppContext:
    """Holds every long-lived resource of the running application."""

    settings: Settings
    db: Database
    cache: MarketCache
    rest_client: PolymarketRestClient
    instrument_repo: InstrumentRepository
    signal_repo: SignalRepository
    decision_repo: DecisionRepository
    system_event_repo: SystemEventRepository
    instrument_service: InstrumentService
    universe_job: UniverseRefreshJob | None = None
    ws_client: PolymarketWsClient | None = None
    market_data: MarketDataService | None = None
    data_quality: DataQualityService | None = None
    books: OrderbookManager | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    extra: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        log = get_logger("bootstrap")
        if self.universe_job is not None:
            await self.universe_job.stop()
        if self.market_data is not None:
            await self.market_data.stop()
        if self.ws_client is not None:
            await self.ws_client.stop()
        await self.rest_client.aclose()
        await self.cache.aclose()
        await self.db.dispose()
        log.info("app_context_closed")


def build_persistence_bundle(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> PersistenceBundle:
    """Wire batch buffers to their repositories (idempotent, bounded, safe)."""
    tick_repo = MarketTickRepository(session_factory)
    candle_repo = CandleRepository(session_factory)
    book_repo = OrderbookSnapshotRepository(session_factory)
    funding_repo = FundingRateRepository(session_factory)
    batch = settings.ws.persist_batch_size
    interval = settings.ws.persist_flush_seconds

    async def flush_ticks(records: list[tuple[int, TickerData]]) -> None:
        await tick_repo.add_ticks(records)

    async def flush_candles(records: list[tuple[int, CandleData]]) -> None:
        grouped: dict[int, list[CandleData]] = {}
        for instrument_pk, candle in records:
            grouped.setdefault(instrument_pk, []).append(candle)
        for instrument_pk, candles in grouped.items():
            await candle_repo.upsert_candles(instrument_pk, candles, DataSource.WEBSOCKET)

    async def flush_books(records: list[dict[str, Any]]) -> None:
        await book_repo.add_snapshots(records)

    async def flush_funding(records: list[tuple[int, FundingRateData]]) -> None:
        await funding_repo.add_rates(records)

    return PersistenceBundle(
        ticks=PersistenceBuffer(
            "market_ticks",
            flush_ticks,
            batch_size=batch,
            flush_interval_seconds=interval,
            # sample: at most one persisted tick per instrument per 5s bucket
            dedup_key=lambda item: (item[0], int(item[1].ts.timestamp()) // 5),
        ),
        candles=PersistenceBuffer(
            "candles",
            flush_candles,
            batch_size=batch,
            flush_interval_seconds=interval,
            # keep only the latest state of each forming candle
            dedup_key=lambda item: (item[0], item[1].timeframe.value, item[1].open_time),
        ),
        books=PersistenceBuffer(
            "orderbook_snapshots",
            flush_books,
            batch_size=batch,
            flush_interval_seconds=interval,
            # at most one snapshot per instrument per minute
            dedup_key=lambda record: (
                record["instrument_pk"],
                record["ts"].replace(second=0, microsecond=0),
            ),
        ),
        funding=PersistenceBuffer(
            "funding_rates",
            flush_funding,
            batch_size=batch,
            flush_interval_seconds=interval,
            dedup_key=lambda item: (item[0], item[1].ts),
        ),
    )


async def build_context(settings: Settings) -> AppContext:
    configure_logging(settings.app.log_level)
    log = get_logger("bootstrap")

    db = Database(settings.database)
    cache = MarketCache.from_settings(settings.redis)
    rest_client = PolymarketRestClient(settings.polymarket)

    instrument_repo = InstrumentRepository(db.session_factory)
    signal_repo = SignalRepository(db.session_factory)
    decision_repo = DecisionRepository(db.session_factory)
    system_event_repo = SystemEventRepository(db.session_factory)

    instrument_service = InstrumentService(rest_client, instrument_repo, settings.universe)

    ws_client: PolymarketWsClient | None = None
    market_data: MarketDataService | None = None
    data_quality: DataQualityService | None = None
    books: OrderbookManager | None = None
    on_refresh = None

    if settings.ws.enabled:
        books = OrderbookManager()
        buffers = build_persistence_bundle(db.session_factory, settings)
        ws_client = PolymarketWsClient(settings.ws)
        market_data = MarketDataService(
            ws_client,
            cache,
            books,
            buffers,
            settings.ws,
            settings.data_quality,
            system_event_repo,
        )
        ws_client.set_state_listener(market_data.on_ws_state)
        data_quality = DataQualityService(
            market_data,
            ws_client,
            books,
            settings.freshness,
            settings.data_quality,
            cache_degraded=lambda: cache.degraded,
        )

        async def _sync_after_refresh(_: UniverseRefreshResult) -> None:
            rows = await instrument_repo.list_enabled()
            assert market_data is not None
            await market_data.sync_universe(
                [(row.id, row.instrument_id, row.symbol) for row in rows]
            )

        on_refresh = _sync_after_refresh
        await ws_client.start()
        await market_data.start()

    universe_job: UniverseRefreshJob | None = None
    if settings.universe.refresh_enabled:
        universe_job = UniverseRefreshJob(instrument_service, settings.universe, on_refresh)
        await universe_job.start()

    log.info(
        "app_context_built",
        environment=settings.app.environment,
        kill_switch=settings.app.kill_switch,
        universe_refresh_enabled=settings.universe.refresh_enabled,
        websocket_enabled=settings.ws.enabled,
    )
    return AppContext(
        settings=settings,
        db=db,
        cache=cache,
        rest_client=rest_client,
        instrument_repo=instrument_repo,
        signal_repo=signal_repo,
        decision_repo=decision_repo,
        system_event_repo=system_event_repo,
        instrument_service=instrument_service,
        universe_job=universe_job,
        ws_client=ws_client,
        market_data=market_data,
        data_quality=data_quality,
        books=books,
    )
