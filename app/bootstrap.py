"""Application wiring: builds and tears down the process-wide AppContext."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.adapters.polymarket_rest import PolymarketRestClient
from app.config import Settings
from app.data.instrument_service import InstrumentService
from app.data.market_cache import MarketCache
from app.jobs.universe_refresh import UniverseRefreshJob
from app.observability.logging import configure_logging, get_logger
from app.repositories.database import Database
from app.repositories.decision_repository import DecisionRepository
from app.repositories.instrument_repository import InstrumentRepository
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
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    extra: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        log = get_logger("bootstrap")
        if self.universe_job is not None:
            await self.universe_job.stop()
        await self.rest_client.aclose()
        await self.cache.aclose()
        await self.db.dispose()
        log.info("app_context_closed")


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

    universe_job: UniverseRefreshJob | None = None
    if settings.universe.refresh_enabled:
        universe_job = UniverseRefreshJob(instrument_service, settings.universe)
        await universe_job.start()

    log.info(
        "app_context_built",
        environment=settings.app.environment,
        kill_switch=settings.app.kill_switch,
        universe_refresh_enabled=settings.universe.refresh_enabled,
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
    )
