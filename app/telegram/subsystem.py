"""Telegram subsystem facade: owns lifecycle and reports aggregate state.

States:
- DISABLED     - TELEGRAM_ENABLED=false (safe default): no client, no polling
- DEGRADED     - enabled but missing/invalid configuration, worker not alive,
                 or bot-state persistence degraded; the data engine runs on
- UNAVAILABLE  - the API rejected the token (401): nothing can be delivered
- HEALTHY      - client, worker and (if enabled) polling are up
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters.telegram import TelegramPollingService
from app.config import TelegramSettings
from app.domain.enums import TelegramSubsystemState
from app.repositories.telegram_delivery_repository import TelegramDeliveryRepository
from app.telegram.client import TelegramClient
from app.telegram.delivery_queue import DeliveryQueueWorker
from app.telegram.delivery_service import TelegramDeliveryService
from app.telegram.formatter import TelegramFormatter
from app.telegram.rate_limiter import TelegramRateLimiter


@dataclass
class TelegramSubsystem:
    settings: TelegramSettings
    repository: TelegramDeliveryRepository | None = None
    delivery_service: TelegramDeliveryService | None = None
    client: TelegramClient | None = None
    worker: DeliveryQueueWorker | None = None
    polling: TelegramPollingService | None = None
    formatter: TelegramFormatter | None = None
    rate_limiter: TelegramRateLimiter | None = None
    configured: bool = False

    @property
    def state(self) -> TelegramSubsystemState:
        if not self.settings.enabled:
            return TelegramSubsystemState.DISABLED
        if not self.configured or self.client is None or self.worker is None:
            return TelegramSubsystemState.DEGRADED
        if self.worker.auth_failed or (self.polling is not None and self.polling.auth_failed):
            return TelegramSubsystemState.UNAVAILABLE
        if not self.worker.alive:
            return TelegramSubsystemState.DEGRADED
        if self.settings.commands_enabled and self.polling is not None and not self.polling.alive:
            return TelegramSubsystemState.DEGRADED
        return TelegramSubsystemState.HEALTHY

    async def start(self) -> None:
        if self.worker is not None:
            await self.worker.start()
        if self.polling is not None:
            await self.polling.start()

    async def stop(self) -> None:
        if self.polling is not None:
            await self.polling.stop()
        if self.worker is not None:
            await self.worker.stop()
        if self.client is not None:
            await self.client.aclose()

    async def health_stats(self) -> dict[str, Any]:
        """Safe aggregate for /health - no tokens, no chat ids."""
        stats: dict[str, Any] = {
            "enabled": self.settings.enabled,
            "state": self.state.value,
        }
        if self.repository is not None:
            try:
                stats["queue_depth"] = await self.repository.open_count()
                stats["dead_letter_count"] = await self.repository.dead_letter_count()
                last_success = await self.repository.last_success_at()
                stats["last_success_at"] = last_success.isoformat() if last_success else None
            except Exception:
                stats["queue_depth"] = None
                stats["dead_letter_count"] = None
                stats["last_success_at"] = None
        if self.worker is not None:
            stats["worker_alive"] = self.worker.alive
        return stats

    async def status_stats(self) -> dict[str, Any]:
        """Safe aggregate for /status - classified info only."""
        stats = await self.health_stats()
        stats["polling_alive"] = self.polling.alive if self.polling is not None else False
        stats["commands_enabled"] = self.settings.commands_enabled
        if self.rate_limiter is not None:
            stats["rate_limit_wait_seconds_total"] = round(self.rate_limiter.total_wait_seconds, 2)
        if self.repository is not None:
            try:
                stats["delivery_counts"] = await self.repository.counts_by_status()
            except Exception:
                stats["delivery_counts"] = None
        return stats
