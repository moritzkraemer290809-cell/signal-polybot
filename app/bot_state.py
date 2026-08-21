"""Global runtime bot state (ACTIVE / PAUSED), persisted across restarts.

The static ``APP_KILL_SWITCH`` env flag and the runtime pause state are OR-ed:
either one pauses the bot.  Pausing stops non-critical Telegram deliveries and
(later) scanner/strategy activity; it never stops health checks, the data
engine, or critical system alerts.

Resilience: if the database is unavailable the last known state is kept
in memory and the service reports itself degraded - a DB outage never blocks
pause/resume commands or crashes the process.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.observability.logging import get_logger
from app.repositories.app_state_repository import AppStateRepository

_STATE_KEY = "bot_paused"


class BotStateService:
    def __init__(self, repository: AppStateRepository, kill_switch: bool = False) -> None:
        self._repository = repository
        self._kill_switch = kill_switch
        self._log = get_logger("bot_state")
        self._cached_paused = False
        self._change_counter = 0
        self._loaded = False
        self.degraded = False

    async def load(self) -> None:
        """Restore the persisted state (called once at startup)."""
        try:
            value = await self._repository.get(_STATE_KEY)
        except Exception as exc:
            self.degraded = True
            self._log.warning("bot_state_load_failed", error=type(exc).__name__)
            self._loaded = True
            return
        self.degraded = False
        if value is not None:
            self._cached_paused = bool(value.get("paused", False))
            self._change_counter = int(value.get("change_counter", 0))
        self._loaded = True

    async def is_paused(self) -> bool:
        """Effective pause state (kill switch OR runtime pause)."""
        if not self._loaded:
            await self.load()
        return self._kill_switch or self._cached_paused

    @property
    def runtime_paused(self) -> bool:
        return self._cached_paused

    @property
    def change_counter(self) -> int:
        return self._change_counter

    async def set_paused(self, paused: bool, changed_by: int | None = None) -> bool:
        """Set the runtime pause state.  Returns True when the state changed.

        Idempotent: setting the current state again is a no-op and no error.
        """
        if not self._loaded:
            await self.load()
        if self._cached_paused == paused:
            return False
        self._cached_paused = paused
        self._change_counter += 1
        payload = {
            "paused": paused,
            "change_counter": self._change_counter,
            "changed_at": datetime.now(tz=UTC).isoformat(),
            "changed_by": changed_by,
        }
        try:
            await self._repository.set(_STATE_KEY, payload)
            self.degraded = False
        except Exception as exc:
            # keep the in-memory state; persistence catches up on next change
            self.degraded = True
            self._log.error("bot_state_persist_failed", error=type(exc).__name__)
        return True
