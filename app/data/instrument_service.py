"""Dynamic instrument discovery for the configured market universe.

Instrument ids are never hard-coded: symbols from configuration are resolved
against the live ``/v1/info/instruments`` payload on every refresh and synced
into the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.config import UniverseSettings
from app.domain.models import InstrumentMeta, utc_now
from app.observability.logging import get_logger
from app.repositories.instrument_repository import InstrumentRepository


class InstrumentSource(Protocol):
    """Minimal interface the service needs (satisfied by the REST client)."""

    async def get_instruments(self, *, use_cache: bool = True) -> list[InstrumentMeta]: ...


@dataclass
class UniverseRefreshResult:
    discovered_total: int = 0
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    delisted: list[str] = field(default_factory=list)
    enabled_symbols: list[str] = field(default_factory=list)
    missing_configured_symbols: list[str] = field(default_factory=list)
    refreshed_at: datetime | None = None


class InstrumentService:
    def __init__(
        self,
        source: InstrumentSource,
        repository: InstrumentRepository,
        universe: UniverseSettings,
    ) -> None:
        self._source = source
        self._repository = repository
        self._universe = universe
        self._log = get_logger("instrument_service")
        self.last_refresh_at: datetime | None = None
        self.last_result: UniverseRefreshResult | None = None

    async def refresh(self) -> UniverseRefreshResult:
        """Discover instruments and sync them into the database."""
        discovered = await self._source.get_instruments(use_cache=False)
        configured = self._universe.all_symbols
        discovered_symbols = {meta.symbol for meta in discovered}
        missing = sorted(configured - discovered_symbols)
        if missing:
            self._log.warning(
                "configured_symbols_not_discovered",
                missing_symbols=missing,
                discovered_count=len(discovered),
            )

        upsert = await self._repository.upsert_discovered(discovered, configured)
        result = UniverseRefreshResult(
            discovered_total=len(discovered),
            created=upsert.created,
            updated=upsert.updated,
            delisted=upsert.delisted,
            enabled_symbols=sorted(upsert.enabled_symbols),
            missing_configured_symbols=missing,
            refreshed_at=utc_now(),
        )
        self.last_refresh_at = result.refreshed_at
        self.last_result = result
        self._log.info(
            "universe_refreshed",
            discovered_total=result.discovered_total,
            created=len(result.created),
            updated=len(result.updated),
            delisted=len(result.delisted),
            enabled=result.enabled_symbols,
        )
        return result
