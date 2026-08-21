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
