"""System alerting abstraction.

Phase <= 4: alerts are logged as structured JSON and persisted as
``system_events``.  A Telegram admin alert sink is added in phase 6 behind the
same protocol - business code never talks to Telegram directly.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.domain.enums import SystemEventLevel
from app.observability.logging import get_logger


class AlertSink(Protocol):
    async def emit(
        self,
        level: SystemEventLevel,
        event_type: str,
        message: str,
        context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None: ...


class TelegramAlertSink:
    """Alert sink that enqueues admin alerts via the persistent delivery queue.

    Never sends synchronously and never raises into the caller; the delivery
    queue applies priorities, dedup and rate limits.
    """

    def __init__(self, delivery_service: Any, enabled: bool = True) -> None:
        self._delivery = delivery_service
        self._enabled = enabled
        self._log = get_logger("alerts")

    async def emit(
        self,
        level: SystemEventLevel,
        event_type: str,
        message: str,
        context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        if not self._enabled or self._delivery is None:
            return
        from app.domain.enums import TelegramDeliveryType
        from app.telegram.models import DeliveryRequest, SystemMessagePayload

        delivery_type = (
            TelegramDeliveryType.SYSTEM_ERROR
            if level in (SystemEventLevel.ERROR, SystemEventLevel.CRITICAL)
            else TelegramDeliveryType.SYSTEM_WARNING
        )
        try:
            await self._delivery.enqueue(
                DeliveryRequest(
                    delivery_type=delivery_type,
                    payload=SystemMessagePayload(title=message, fields={}),
                    idempotency_key=f"alert:{event_type}:{correlation_id or message[:40]}",
                    correlation_id=correlation_id,
                )
            )
        except Exception as exc:
            self._log.warning("telegram_alert_enqueue_failed", error=type(exc).__name__)


class LogAlertSink:
    """Default alert sink: structured log output only."""

    def __init__(self) -> None:
        self._log = get_logger("alerts")

    async def emit(
        self,
        level: SystemEventLevel,
        event_type: str,
        message: str,
        context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self._log.warning(
            "system_alert",
            alert_level=level.value,
            event_type=event_type,
            message=message,
            context=context or {},
            correlation_id=correlation_id,
        )
