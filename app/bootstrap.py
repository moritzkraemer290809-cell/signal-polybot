"""Application wiring: builds and tears down the process-wide AppContext."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.polymarket_rest import PolymarketRestClient
from app.adapters.polymarket_ws import PolymarketWsClient
from app.adapters.telegram import TelegramPollingService
from app.bot_state import BotStateService
from app.config import Settings
from app.data.data_quality import DataQualityService
from app.data.instrument_service import InstrumentService, UniverseRefreshResult
from app.data.market_cache import MarketCache
from app.data.market_data_service import MarketDataService, PersistenceBundle
from app.data.orderbook_service import OrderbookManager
from app.data.persistence_buffer import PersistenceBuffer
from app.domain.enums import (
    DataSource,
    SystemEventLevel,
    TelegramDeliveryType,
    WsConnectionState,
)
from app.domain.models import CandleData, FundingRateData, TickerData
from app.jobs.risk_plan_evaluation_refresh import RiskPlanEvaluationJob
from app.jobs.signal_lifecycle_monitor import SignalLifecycleMonitorJob
from app.jobs.strategy_evaluation_refresh import StrategyEvaluationJob
from app.jobs.universe_refresh import UniverseRefreshJob
from app.observability.logging import configure_logging, get_logger
from app.repositories.app_state_repository import AppStateRepository
from app.repositories.candle_repository import CandleRepository
from app.repositories.cost_estimate_repository import CostEstimateRepository
from app.repositories.database import Database
from app.repositories.decision_repository import DecisionRepository
from app.repositories.feature_repository import FeatureRepository
from app.repositories.fee_schedule_repository import (
    ExecutionAssumptionRepository,
    FeeScheduleRepository,
)
from app.repositories.funding_repository import FundingRateRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.instrument_risk_repository import InstrumentRiskRepository
from app.repositories.market_selection_repository import MarketSelectionRepository
from app.repositories.market_tick_repository import MarketTickRepository
from app.repositories.orderbook_repository import OrderbookSnapshotRepository
from app.repositories.regime_repository import RegimeRepository
from app.repositories.risk_plan_rejection_repository import RiskPlanRejectionRepository
from app.repositories.risk_plan_repository import RiskPlanRepository
from app.repositories.session_event_repository import SessionEventRepository
from app.repositories.setup_candidate_repository import SetupCandidateRepository
from app.repositories.signal_event_repository import SignalEventRepository
from app.repositories.signal_lifecycle_repository import SignalLifecycleRepository
from app.repositories.signal_rejection_repository import SignalRejectionRepository
from app.repositories.signal_repository import SignalRepository
from app.repositories.signal_update_repository import SignalUpdateRepository
from app.repositories.strategy_decision_repository import StrategyDecisionRepository
from app.repositories.system_event_repository import SystemEventRepository
from app.repositories.telegram_delivery_repository import TelegramDeliveryRepository
from app.repositories.watchlist_repository import WatchlistRepository
from app.risk.evaluation_context import RiskPlanEvaluationContextBuilder
from app.selection.classification import AssetClassifier
from app.selection.market_selection_engine import MarketSelectionService
from app.selection.selection_events import SelectionNotifier
from app.selection.selection_scheduler import MarketSelectionCoordinator
from app.selection.watchlist_service import WatchlistService
from app.sessions.early_close_provider import StaticUsEquityEarlyCloseProvider
from app.sessions.equity_calendar import EquityCalendar
from app.sessions.holiday_provider import StaticUsEquityHolidayProvider
from app.sessions.session_manager import CryptoSessionManager, EquitySessionManager
from app.signals.lifecycle_context import SignalLifecycleContextBuilder
from app.strategy.evaluation_context import EvaluationContextBuilder
from app.strategy.feature_store import FeatureStore
from app.telegram.authorization import TelegramAuthorizer
from app.telegram.client import HttpTelegramClient
from app.telegram.command_router import CommandProviders, TelegramCommandRouter
from app.telegram.delivery_queue import DeliveryQueueWorker
from app.telegram.delivery_service import TelegramDeliveryService
from app.telegram.formatter import TelegramFormatter
from app.telegram.idempotency import startup_key, system_event_key
from app.telegram.models import (
    DailyStatusPayload,
    DeliveryRequest,
    SystemMessagePayload,
)
from app.telegram.rate_limiter import TelegramRateLimiter
from app.telegram.subsystem import TelegramSubsystem


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
    bot_state: BotStateService | None = None
    telegram: TelegramSubsystem | None = None
    equity_sessions: EquitySessionManager | None = None
    crypto_sessions: CryptoSessionManager | None = None
    selection: MarketSelectionCoordinator | None = None
    strategy: StrategyEvaluationJob | None = None
    risk: RiskPlanEvaluationJob | None = None
    signals: SignalLifecycleMonitorJob | None = None
    start_correlation_id: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    extra: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        log = get_logger("bootstrap")
        if self.universe_job is not None:
            await self.universe_job.stop()
        if self.signals is not None:
            await self.signals.stop()
        if self.risk is not None:
            await self.risk.stop()
        if self.strategy is not None:
            await self.strategy.stop()
        if self.selection is not None:
            await self.selection.stop()
        if self.market_data is not None:
            await self.market_data.stop()
        if self.ws_client is not None:
            await self.ws_client.stop()
        if self.telegram is not None:
            await _shutdown_telegram(self)
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


def _status_counts(ctx: AppContext) -> dict[str, int]:
    if ctx.data_quality is None:
        return {}
    counts = ctx.data_quality.summary().get("status_counts")
    if not isinstance(counts, dict):
        return {}
    return {str(key): int(value) for key, value in counts.items()}


async def _shutdown_telegram(ctx: AppContext) -> None:
    """Best-effort shutdown notice, then stop polling/worker/client cleanly."""
    telegram = ctx.telegram
    assert telegram is not None
    if (
        telegram.delivery_service is not None
        and telegram.worker is not None
        and telegram.state.value == "HEALTHY"
    ):
        with contextlib.suppress(Exception):
            await telegram.delivery_service.enqueue(
                DeliveryRequest(
                    delivery_type=TelegramDeliveryType.SYSTEM_SHUTDOWN,
                    payload=SystemMessagePayload(title="Geordneter Shutdown", fields={}),
                    idempotency_key=f"shutdown:{ctx.start_correlation_id}",
                    correlation_id=ctx.start_correlation_id,
                )
            )
        with contextlib.suppress(Exception, TimeoutError):
            await asyncio.wait_for(telegram.worker.process_once(), timeout=5.0)
    await telegram.stop()


def _build_telegram(
    settings: Settings,
    ctx_holder: dict[str, Any],
    db: Database,
    bot_state: BotStateService,
    system_event_repo: SystemEventRepository,
) -> TelegramSubsystem:
    """Wire the Telegram subsystem.  Misconfiguration degrades, never crashes."""
    telegram_settings = settings.telegram
    repository = TelegramDeliveryRepository(db.session_factory)
    subsystem = TelegramSubsystem(settings=telegram_settings, repository=repository)
    if not telegram_settings.configured:
        get_logger("bootstrap").error(
            "telegram_enabled_but_not_configured",
            hint="set TELEGRAM_BOT_TOKEN and TELEGRAM_GROUP_ID",
        )
        return subsystem  # state -> DEGRADED

    formatter = TelegramFormatter(settings.app.display_timezone)
    rate_limiter = TelegramRateLimiter(telegram_settings)
    client = HttpTelegramClient(telegram_settings)

    delivery_service = TelegramDeliveryService(
        repository,
        telegram_settings,
        is_paused=bot_state.is_paused,
    )

    async def on_dead_letter(row: Any) -> None:
        await system_event_repo.add(
            SystemEventLevel.ERROR,
            "telegram_dead_letter",
            f"telegram delivery dead-lettered ({row.message_type})",
            {"delivery_id": str(row.delivery_id), "error_class": row.last_error_class},
        )

    worker = DeliveryQueueWorker(
        repository,
        client,
        formatter,
        rate_limiter,
        telegram_settings,
        on_dead_letter=on_dead_letter,
    )
    delivery_service._wake_worker = worker.wake

    polling: TelegramPollingService | None = None
    if telegram_settings.commands_enabled:

        async def audit(event_type: str, message: str, context: dict[str, Any]) -> None:
            try:
                await system_event_repo.add(SystemEventLevel.WARNING, event_type, message, context)
            except Exception:
                pass

        async def status_snapshot() -> dict[str, Any]:
            ctx: AppContext = ctx_holder["ctx"]
            paused = await bot_state.is_paused()
            quality = _status_counts(ctx)
            return {
                "Bot": "PAUSED" if paused else "ACTIVE",
                "Telegram": subsystem.state.value,
                "WebSocket": ctx.ws_client.state.value if ctx.ws_client else "disabled",
                "Aktive Instrumente": len(ctx.market_data.trackers()) if ctx.market_data else 0,
                "Datenqualität": " · ".join(f"{k}: {v}" for k, v in sorted(quality.items()))
                or "keine Daten",
                "Offene Deliveries": await repository.open_count(),
                "Dead Letter": await repository.dead_letter_count(),
            }

        async def health_snapshot() -> dict[str, Any]:
            ctx: AppContext = ctx_holder["ctx"]
            db_ok = await ctx.db.ping()
            redis_ok = await ctx.cache.ping()
            ws_state = ctx.ws_client.state.value if ctx.ws_client else "disabled"
            summary = ctx.data_quality.summary() if ctx.data_quality else {"stale_count": 0}
            stale = int(summary.get("stale_count", 0) or 0)  # type: ignore[call-overload]
            if not db_ok:
                overall = "UNAVAILABLE"
            elif not redis_ok or stale > 0 or subsystem.state.value != "HEALTHY":
                overall = "DEGRADED"
            else:
                overall = "HEALTHY"
            return {
                "overall": overall,
                "API": "ok",
                "Datenbank": "ok" if db_ok else "nicht erreichbar",
                "Redis": "ok" if redis_ok else "nicht erreichbar",
                "WebSocket": ws_state,
                "Telegram": subsystem.state.value,
                "Stale Assets": stale,
            }

        async def daily_snapshot() -> DailyStatusPayload:
            ctx: AppContext = ctx_holder["ctx"]
            uptime = (datetime.now(tz=UTC) - ctx.started_at).total_seconds()
            quality = _status_counts(ctx)
            try:
                delivery_counts = await repository.counts_by_status()
            except Exception:
                delivery_counts = {}
            return DailyStatusPayload(
                uptime_seconds=uptime,
                ws_reconnects=ctx.ws_client.reconnect_count if ctx.ws_client else 0,
                invalid_events=(ctx.market_data.total_invalid_events() if ctx.market_data else 0),
                quality_counts={str(k): int(v) for k, v in quality.items()},
                delivery_counts=delivery_counts,
            )

        authorizer = TelegramAuthorizer(telegram_settings, audit=audit)
        router = TelegramCommandRouter(
            telegram_settings,
            authorizer,
            bot_state,
            delivery_service,
            CommandProviders(
                status_snapshot=status_snapshot,
                health_snapshot=health_snapshot,
                daily_snapshot=daily_snapshot,
            ),
            audit=audit,
        )
        polling = TelegramPollingService(client, router, rate_limiter, telegram_settings)

    subsystem.delivery_service = delivery_service
    subsystem.client = client
    subsystem.worker = worker
    subsystem.polling = polling
    subsystem.formatter = formatter
    subsystem.rate_limiter = rate_limiter
    subsystem.configured = True
    return subsystem


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

    bot_state = BotStateService(
        AppStateRepository(db.session_factory), kill_switch=settings.app.kill_switch
    )
    await bot_state.load()

    start_correlation_id = uuid.uuid4().hex
    ctx_holder: dict[str, Any] = {}
    telegram: TelegramSubsystem | None = None
    if settings.telegram.enabled:
        telegram = _build_telegram(settings, ctx_holder, db, bot_state, system_event_repo)

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
        _market_data = market_data

        async def _ws_state_listener(state: WsConnectionState) -> None:
            await _market_data.on_ws_state(state)
            if (
                telegram is not None
                and telegram.delivery_service is not None
                and settings.telegram.system_alerts_enabled
                and state is WsConnectionState.DEGRADED
            ):
                with contextlib.suppress(Exception):
                    await telegram.delivery_service.enqueue(
                        DeliveryRequest(
                            delivery_type=TelegramDeliveryType.WEBSOCKET_DEGRADED,
                            payload=SystemMessagePayload(
                                title="WebSocket-Verbindung beeinträchtigt",
                                fields={"Status": state.value},
                                note="Reconnects laufen weiter; Signale sind blockiert, "
                                "bis die Datenqualität wieder HEALTHY ist.",
                            ),
                            idempotency_key=system_event_key(
                                TelegramDeliveryType.WEBSOCKET_DEGRADED,
                                start_correlation_id,
                            ),
                            correlation_id=start_correlation_id,
                        )
                    )

        ws_client.set_state_listener(_ws_state_listener)
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
            ctx_after = ctx_holder.get("ctx")
            if ctx_after is not None and ctx_after.selection is not None:
                ctx_after.selection.trigger()
            if ctx_after is not None and ctx_after.strategy is not None:
                ctx_after.strategy.trigger()
            if ctx_after is not None and ctx_after.risk is not None:
                ctx_after.risk.trigger()
            if ctx_after is not None and ctx_after.signals is not None:
                ctx_after.signals.trigger()

        on_refresh = _sync_after_refresh
        await ws_client.start()
        await market_data.start()

    # --- sessions + market selection (phase 7) ------------------------------
    equity_calendar = EquityCalendar(
        settings.equity_sessions,
        StaticUsEquityHolidayProvider(),
        StaticUsEquityEarlyCloseProvider(),
    )
    equity_sessions = EquitySessionManager(settings.equity_sessions, equity_calendar)
    crypto_sessions = CryptoSessionManager(settings.crypto_sessions)

    selection: MarketSelectionCoordinator | None = None
    if settings.selection.enabled:
        selection_repo = MarketSelectionRepository(db.session_factory)
        watchlist_repo = WatchlistRepository(db.session_factory)
        session_event_repo = SessionEventRepository(db.session_factory)
        notifier = SelectionNotifier(
            settings.selection,
            system_event_repo,
            telegram.delivery_service if telegram is not None else None,
            bot_state,
        )
        selection_service = MarketSelectionService(
            settings.selection,
            settings.thresholds,
            AssetClassifier(settings.selection),
            market_data,
            data_quality,
            books,
            configuration_version=settings.app.config_version,
        )
        watchlist_service = WatchlistService(watchlist_repo, settings.selection, notifier)
        selection = MarketSelectionCoordinator(
            settings.selection,
            selection_service,
            watchlist_service,
            equity_sessions,
            crypto_sessions,
            instrument_repo,
            selection_repo,
            session_event_repo,
            notifier,
        )
        await selection.start()

    strategy_job: StrategyEvaluationJob | None = None
    if settings.strategy.enabled:
        candle_repo = CandleRepository(db.session_factory)
        context_builder = EvaluationContextBuilder(
            settings.strategy, candle_repo, market_data, data_quality, books
        )
        feature_store = FeatureStore(
            FeatureRepository(db.session_factory),
            RegimeRepository(db.session_factory),
            settings.strategy,
        )
        strategy_job = StrategyEvaluationJob(
            settings.strategy,
            context_builder,
            feature_store,
            SetupCandidateRepository(db.session_factory),
            StrategyDecisionRepository(db.session_factory),
            candle_repo,
            instrument_repo,
            selection,
            bot_state,
        )
        await strategy_job.start()

    risk_job: RiskPlanEvaluationJob | None = None
    if settings.risk.engine_enabled:
        risk_candle_repo = CandleRepository(db.session_factory)
        risk_builder = RiskPlanEvaluationContextBuilder(
            settings.risk,
            settings.costs,
            risk_candle_repo,
            FeatureRepository(db.session_factory),
            FundingRateRepository(db.session_factory),
            market_data,
            data_quality,
            books,
        )
        risk_job = RiskPlanEvaluationJob(
            settings.risk,
            settings.costs,
            risk_builder,
            RiskPlanRepository(db.session_factory),
            RiskPlanRejectionRepository(db.session_factory),
            CostEstimateRepository(db.session_factory),
            InstrumentRiskRepository(db.session_factory),
            FeeScheduleRepository(db.session_factory),
            ExecutionAssumptionRepository(db.session_factory),
            SetupCandidateRepository(db.session_factory),
            instrument_repo,
            selection,
            bot_state,
        )
        await risk_job.start()

    signals_job: SignalLifecycleMonitorJob | None = None
    if settings.signals.lifecycle_enabled:
        signals_builder = SignalLifecycleContextBuilder(
            settings.signals,
            RiskPlanRepository(db.session_factory),
            SetupCandidateRepository(db.session_factory),
            CandleRepository(db.session_factory),
            FeatureRepository(db.session_factory),
            InstrumentRiskRepository(db.session_factory),
            market_data,
            data_quality,
            books,
        )
        signals_job = SignalLifecycleMonitorJob(
            settings.signals,
            signals_builder,
            SignalLifecycleRepository(db.session_factory),
            SignalEventRepository(db.session_factory),
            SignalUpdateRepository(db.session_factory),
            SignalRejectionRepository(db.session_factory),
            RiskPlanRepository(db.session_factory),
            selection,
            bot_state,
        )
        await signals_job.start()

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
        telegram_enabled=settings.telegram.enabled,
        telegram_state=telegram.state.value if telegram else "DISABLED",
    )
    ctx = AppContext(
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
        bot_state=bot_state,
        telegram=telegram,
        equity_sessions=equity_sessions,
        crypto_sessions=crypto_sessions,
        selection=selection,
        strategy=strategy_job,
        risk=risk_job,
        signals=signals_job,
        start_correlation_id=start_correlation_id,
    )
    ctx_holder["ctx"] = ctx

    if telegram is not None:
        await telegram.start()
        if telegram.delivery_service is not None:
            with contextlib.suppress(Exception):
                await telegram.delivery_service.enqueue(
                    DeliveryRequest(
                        delivery_type=TelegramDeliveryType.SYSTEM_STARTUP,
                        payload=SystemMessagePayload(
                            title="",
                            fields={
                                "Modus": "Read-only Market Data",
                                "Trading": "deaktiviert",
                                "Telegram": "aktiv",
                                "WebSocket": (
                                    "aktiviert" if settings.ws.enabled else "deaktiviert"
                                ),
                                "Universum": ", ".join(sorted(settings.universe.all_symbols)),
                            },
                        ),
                        idempotency_key=startup_key(start_correlation_id),
                        correlation_id=start_correlation_id,
                    )
                )
        elif settings.telegram.enabled:
            with contextlib.suppress(Exception):
                await system_event_repo.add(
                    SystemEventLevel.WARNING,
                    "telegram_degraded",
                    "telegram enabled but not configured",
                    {},
                )
    return ctx
