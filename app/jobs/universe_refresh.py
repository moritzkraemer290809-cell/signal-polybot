"""Periodic universe refresh job (instrument discovery)."""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable

from app.config import UniverseSettings
from app.data.instrument_service import InstrumentService, UniverseRefreshResult
from app.observability.logging import get_logger

RefreshListener = Callable[[UniverseRefreshResult], Awaitable[None]]


class UniverseRefreshJob:
    """Runs :meth:`InstrumentService.refresh` periodically with jitter.

    ``on_refresh`` lets the market data layer reconcile its WebSocket
    subscriptions after every successful refresh (idempotent sub/unsub,
    including delistings).
    """

    def __init__(
        self,
        service: InstrumentService,
        settings: UniverseSettings,
        on_refresh: RefreshListener | None = None,
    ) -> None:
        self._service = service
        self._settings = settings
        self._on_refresh = on_refresh
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("universe_refresh_job")

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="universe_refresh")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                result = await self._service.refresh()
                if self._on_refresh is not None:
                    await self._on_refresh(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # refresh must never kill the process
                self._log.error("universe_refresh_failed", error=str(exc))
            delay = self._settings.refresh_interval_seconds + random.uniform(
                0, self._settings.refresh_jitter_seconds
            )
            await asyncio.sleep(delay)
