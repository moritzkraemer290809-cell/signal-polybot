"""Selection/session event notifications.

Optional, disabled by default, rate-limited and deduplicated via the phase-6
delivery queue.  Messages are purely technical: they never contain a market
direction, a recommendation or any trading terminology.  While the bot is in
the global PAUSED mode, all (optional, non-critical) selection notices are
suppressed.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.config import MarketSelectionSettings
from app.domain.enums import SystemEventLevel, TelegramDeliveryType
from app.observability.logging import get_logger
from app.observability.metrics import metrics


class _DeliveryService(Protocol):
    async def enqueue(self, request: Any) -> Any: ...


class _BotState(Protocol):
    async def is_paused(self) -> bool: ...


class _SystemEvents(Protocol):
    async def add(
        self,
        level: SystemEventLevel,
        event_type: str,
        message: str,
        context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None: ...


class SelectionNotifier:
    def __init__(
        self,
        settings: MarketSelectionSettings,
        system_events: _SystemEvents | None,
        delivery_service: _DeliveryService | None,
        bot_state: _BotState | None,
    ) -> None:
        self._settings = settings
        self._system_events = system_events
        self._delivery = delivery_service
        self._bot_state = bot_state
        self._log = get_logger("selection_events")

    async def _persist(self, event_type: str, message: str, context: dict[str, Any]) -> None:
        if self._system_events is None:
            return
        try:
            await self._system_events.add(SystemEventLevel.INFO, event_type, message, context)
        except Exception as exc:
            self._log.warning("selection_event_persist_failed", error=type(exc).__name__)

    async def _notify(
        self,
        delivery_type: TelegramDeliveryType,
        title: str,
        fields: dict[str, str],
        idempotency_key: str,
        note: str | None = None,
    ) -> None:
        """Optional technical notice - suppressed while globally paused."""
        if self._delivery is None:
            return
        if self._bot_state is not None and await self._bot_state.is_paused():
            return
        from app.telegram.models import DeliveryRequest, SystemMessagePayload

        try:
            await self._delivery.enqueue(
                DeliveryRequest(
                    delivery_type=delivery_type,
                    payload=SystemMessagePayload(title=title, fields=fields, note=note),
                    idempotency_key=idempotency_key,
                )
            )
        except Exception as exc:
            self._log.warning("selection_notice_failed", error=type(exc).__name__)

    async def session_transition(
        self, scope: str, from_state: str | None, to_state: str, detail: str | None
    ) -> None:
        metrics.increment("selection.session_transitions")
        await self._persist(
            "session_transition",
            f"{scope} session {from_state} -> {to_state}",
            {"scope": scope, "from": from_state, "to": to_state, "detail": detail},
        )
        if self._settings.notify_session_changes:
            await self._notify(
                TelegramDeliveryType.SYSTEM_WARNING,
                f"{scope.capitalize()}-Session: {to_state}",
                {"Vorher": from_state or "-", "Jetzt": to_state},
                idempotency_key=f"session:{scope}:{from_state}->{to_state}:{detail or ''}"[:128],
                note=detail,
            )

    async def watchlist_paused(
        self, symbol: str, status: str, detail: str, decision_id: str
    ) -> None:
        metrics.increment("selection.watchlist_paused")
        await self._persist(
            "watchlist_paused",
            f"{symbol} paused ({status})",
            {"symbol": symbol, "status": status, "detail": detail},
        )
        if self._settings.notify_state_changes:
            await self._notify(
                TelegramDeliveryType.DATA_QUALITY_WARNING,
                f"{symbol} vorübergehend pausiert",
                {"Status": status, "Grund": detail or "siehe Dashboard"},
                idempotency_key=f"watchlist_paused:{decision_id}",
                note="Keine technische Analyse, bis valide Daten wieder verfügbar sind.",
            )

    async def watchlist_restored(self, symbol: str, decision_id: str) -> None:
        metrics.increment("selection.watchlist_restored")
        await self._persist("watchlist_restored", f"{symbol} restored", {"symbol": symbol})
        if self._settings.notify_state_changes:
            await self._notify(
                TelegramDeliveryType.SYSTEM_WARNING,
                f"{symbol} wieder technisch aktiv",
                {"Status": "WATCHLIST_ACTIVE"},
                idempotency_key=f"watchlist_restored:{decision_id}",
            )

    async def calendar_unavailable(self, detail: str) -> None:
        metrics.increment("selection.calendar_unavailable")
        await self._persist("calendar_unavailable", detail, {})
        if self._settings.notify_session_changes:
            await self._notify(
                TelegramDeliveryType.SYSTEM_WARNING,
                "Equity-Kalender nicht verfügbar",
                {"Auswirkung": "Equity-Analyse pausiert"},
                idempotency_key=f"calendar_unavailable:{detail[:80]}",
                note=detail,
            )
