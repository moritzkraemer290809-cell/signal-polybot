"""High-level Telegram delivery API for the rest of the application.

Everything goes through the persistent queue - no message is ever sent
synchronously from a strategy or data callback.  This service applies:

- idempotency (DB-unique key; already-sent deliveries are never re-sent)
- content deduplication within a configurable window
- pause gating (while paused, only priority 1-2 and pause/resume notices)
- queue overflow policy (drop low-priority first, protect priority 1-2)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.config import TelegramSettings
from app.domain.enums import (
    TelegramDeliveryOperation,
    TelegramDeliveryStatus,
    TelegramDeliveryType,
)
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.orm import TelegramDelivery
from app.repositories.telegram_delivery_repository import TelegramDeliveryRepository
from app.telegram.models import DeliveryRequest, content_hash, payload_to_json

_PAUSE_EXEMPT_TYPES = {
    TelegramDeliveryType.BOT_PAUSED,
    TelegramDeliveryType.BOT_RESUMED,
}


class TelegramDeliveryService:
    def __init__(
        self,
        repository: TelegramDeliveryRepository,
        settings: TelegramSettings,
        *,
        is_paused: Callable[[], Awaitable[bool]] | None = None,
        wake_worker: Callable[[], None] | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._is_paused = is_paused
        self._wake_worker = wake_worker
        self._now = now_fn
        self._log = get_logger("telegram_delivery_service")

    async def enqueue(self, request: DeliveryRequest) -> TelegramDelivery | None:
        """Persist a delivery request.  Returns the row, or None when gated.

        Raises ``ValueError`` for structurally invalid requests (EDIT without
        message_id, no resolvable chat id).
        """
        if request.operation is TelegramDeliveryOperation.EDIT and request.message_id is None:
            raise ValueError("EDIT delivery requires a message_id")
        chat_id = request.chat_id if request.chat_id is not None else self._settings.group_id
        if chat_id is None:
            raise ValueError("no chat_id given and no group configured")

        # pause gating: while paused, only critical/high and pause notices pass
        if (
            self._is_paused is not None
            and request.priority > 2
            and request.delivery_type not in _PAUSE_EXEMPT_TYPES
            and await self._is_paused()
        ):
            self._log.info(
                "delivery_gated_paused",
                delivery_type=request.delivery_type.value,
                idempotency_key=request.idempotency_key,
            )
            return None

        digest = content_hash(request.delivery_type, request.payload)
        base_fields = {
            "message_type": request.delivery_type.value,
            "operation": request.operation.value,
            "chat_id": chat_id,
            "message_id": request.message_id,
            "priority": request.priority,
            "payload": payload_to_json(request.payload),
            "content_hash": digest,
            "idempotency_key": request.idempotency_key,
            "scheduled_at": request.scheduled_at,
            "correlation_id": request.correlation_id,
            "signal_id": request.signal_id,
            "system_event_id": request.system_event_id,
        }

        # deduplication window: identical content recently queued/sent -> skip
        if request.dedup and self._settings.deduplication_window_seconds > 0:
            since = self._now() - timedelta(seconds=self._settings.deduplication_window_seconds)
            duplicate = await self._repository.find_recent_duplicate(digest, since)
            if duplicate is not None and duplicate.idempotency_key != request.idempotency_key:
                row, created = await self._repository.insert_if_new(
                    {**base_fields, "status": TelegramDeliveryStatus.SKIPPED_DUPLICATE.value}
                )
                if created:
                    metrics.increment("telegram.skipped_duplicate")
                return row

        # queue overflow policy
        open_count = await self._repository.open_count()
        if open_count >= self._settings.delivery_queue_size:
            freed = await self._repository.cancel_lowest_priority(request.priority)
            if freed:
                await self._log_overflow(dropped=freed)
            elif request.priority >= 3:
                row, created = await self._repository.insert_if_new(
                    {**base_fields, "status": TelegramDeliveryStatus.CANCELLED.value}
                )
                if created:
                    await self._log_overflow(rejected=1)
                return row
            # priority 1-2 is enqueued even beyond the cap - never rejected

        row, created = await self._repository.insert_if_new(
            {**base_fields, "status": TelegramDeliveryStatus.PENDING.value}
        )
        if created:
            metrics.increment("telegram.queued")
            self._log.info(
                "delivery_queued",
                delivery_id=str(row.delivery_id),
                delivery_type=request.delivery_type.value,
                priority=request.priority,
                operation=request.operation.value,
                correlation_id=request.correlation_id,
            )
            if self._wake_worker is not None:
                self._wake_worker()
        return row

    async def _log_overflow(self, dropped: int = 0, rejected: int = 0) -> None:
        metrics.increment("telegram.queue_overflow")
        self._log.warning("delivery_queue_overflow", dropped=dropped, rejected=rejected)
