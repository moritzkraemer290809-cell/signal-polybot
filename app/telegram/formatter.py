"""Safe HTML message formatting for Telegram.

Rules:
- HTML parse mode; every interpolated value is escaped
- hard length cap (Telegram limit 4096) with safe truncation
- no raw exceptions, stack traces, tokens, URLs, DSNs or redis keys
- timestamps rendered in the configured user display timezone (storage is UTC)
- system messages are short and non-alarmist
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.domain.enums import TelegramDeliveryType
from app.telegram.models import (
    DailyStatusPayload,
    Payload,
    SignalPayload,
    SystemMessagePayload,
    WatchlistPayload,
)

MAX_MESSAGE_LENGTH = 4096
_TRUNCATION_SUFFIX = "\n… (gekürzt)"

_TYPE_HEADERS: dict[TelegramDeliveryType, str] = {
    TelegramDeliveryType.SYSTEM_STARTUP: "🟢 <b>PolySignal Intelligence gestartet</b>",
    TelegramDeliveryType.SYSTEM_SHUTDOWN: "⚪️ <b>PolySignal Intelligence gestoppt</b>",
    TelegramDeliveryType.SYSTEM_ERROR: "🔴 <b>Systemfehler</b>",
    TelegramDeliveryType.SYSTEM_WARNING: "🟠 <b>Systemwarnung</b>",
    TelegramDeliveryType.DATA_QUALITY_WARNING: "🟠 <b>Datenqualität eingeschränkt</b>",
    TelegramDeliveryType.DATA_STALE: "🟠 <b>Marktdaten veraltet</b>",
    TelegramDeliveryType.WEBSOCKET_RECONNECTING: "🟡 <b>WebSocket verbindet neu</b>",
    TelegramDeliveryType.WEBSOCKET_DEGRADED: "🟠 <b>WebSocket-Verbindung beeinträchtigt</b>",
    TelegramDeliveryType.BOT_PAUSED: "⏸ <b>Bot pausiert</b>",
    TelegramDeliveryType.BOT_RESUMED: "▶️ <b>Bot fortgesetzt</b>",
    TelegramDeliveryType.DAILY_STATUS: "📊 <b>Tagesstatus</b>",
    TelegramDeliveryType.WATCHLIST: "👀 <b>Watchlist</b>",
}

_SIGNAL_HEADERS: dict[TelegramDeliveryType, str] = {
    TelegramDeliveryType.SIGNAL_OPEN: "Entry ausgelöst",
    TelegramDeliveryType.SIGNAL_UPDATE: "Update",
    TelegramDeliveryType.SIGNAL_PARTIAL: "Teilgewinn",
    TelegramDeliveryType.SIGNAL_BREAK_EVEN: "Stop auf Break-even",
    TelegramDeliveryType.SIGNAL_TRAILING: "Trailing-Stop aktualisiert",
    TelegramDeliveryType.SIGNAL_EXIT: "Position geschlossen",
    TelegramDeliveryType.SIGNAL_STOP: "Stop Loss",
    TelegramDeliveryType.SIGNAL_INVALIDATED: "Setup invalidiert",
    TelegramDeliveryType.SIGNAL_EXPIRED: "Signal abgelaufen",
}


def escape(value: object) -> str:
    return html.escape(str(value), quote=False)


def truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


class TelegramFormatter:
    def __init__(self, display_timezone: str = "Europe/Berlin") -> None:
        try:
            self._tz = ZoneInfo(display_timezone)
        except Exception:
            self._tz = ZoneInfo("UTC")

    def format_time(self, ts: datetime) -> str:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        local = ts.astimezone(self._tz)
        return local.strftime("%d.%m.%Y %H:%M %Z")

    def render(self, delivery_type: TelegramDeliveryType, payload: Payload) -> str:
        if isinstance(payload, SystemMessagePayload):
            text = self._render_system(delivery_type, payload)
        elif isinstance(payload, DailyStatusPayload):
            text = self._render_daily(payload)
        elif isinstance(payload, WatchlistPayload):
            text = self._render_watchlist(payload)
        elif isinstance(payload, SignalPayload):
            text = self._render_signal(delivery_type, payload)
        else:  # pragma: no cover - exhaustive by type
            raise TypeError(f"unsupported payload {type(payload).__name__}")
        return truncate(text)

    # ------------------------------------------------------------- renderers

    def _render_system(
        self, delivery_type: TelegramDeliveryType, payload: SystemMessagePayload
    ) -> str:
        fallback = f"ℹ️ <b>{escape(delivery_type.value)}</b>"  # noqa: RUF001 - info emoji
        header = _TYPE_HEADERS.get(delivery_type, fallback)
        lines = [header, "", f"<b>{escape(payload.title)}</b>"] if payload.title else [header]
        for label, value in payload.fields.items():
            lines.append(f"{escape(label)}: {escape(value)}")
        if payload.note:
            lines.append("")
            lines.append(escape(payload.note))
        return "\n".join(lines)

    def _render_daily(self, payload: DailyStatusPayload) -> str:
        hours, rem = divmod(int(payload.uptime_seconds), 3600)
        minutes = rem // 60
        lines = [
            _TYPE_HEADERS[TelegramDeliveryType.DAILY_STATUS],
            "",
            f"Betriebszeit: {hours}h {minutes:02d}m",
            f"WebSocket-Reconnects: {payload.ws_reconnects}",
            f"Verworfene Datenereignisse: {payload.invalid_events}",
        ]
        if payload.quality_counts:
            quality = " · ".join(
                f"{escape(status)}: {count}"
                for status, count in sorted(payload.quality_counts.items())
            )
            lines.append(f"Datenqualität: {quality}")
        if payload.delivery_counts:
            deliveries = " · ".join(
                f"{escape(status)}: {count}"
                for status, count in sorted(payload.delivery_counts.items())
            )
            lines.append(f"Telegram-Deliveries: {deliveries}")
        lines.append("")
        lines.append("Reines Informationssystem - keine Performance-Aussage.")
        return "\n".join(lines)

    def _render_watchlist(self, payload: WatchlistPayload) -> str:
        return "\n".join(
            [
                _TYPE_HEADERS[TelegramDeliveryType.WATCHLIST],
                "",
                f"<b>{escape(payload.symbol)}</b>",
                escape(payload.note),
            ]
        )

    def _render_signal(self, delivery_type: TelegramDeliveryType, payload: SignalPayload) -> str:
        emoji = "🟢" if payload.direction.upper() == "LONG" else "🔴"
        lines = [f"{emoji} <b>{escape(payload.symbol)} · {escape(payload.direction)}</b>"]
        subheader = _SIGNAL_HEADERS.get(delivery_type)
        if subheader:
            lines.append(f"<b>{subheader}</b>")
        lines.append(f"Signal-ID: {escape(payload.signal_id)}")
        lines.append(f"Status: {escape(payload.status)}")
        lines.append("")
        for label, value in (
            ("Entry", payload.entry),
            ("Stop", payload.stop),
            ("TP1", payload.tp1),
            ("TP2", payload.tp2),
            ("Hebel", payload.leverage),
            ("Netto-CRV", payload.net_rr),
            ("Gültig bis", payload.expires_at),
        ):
            if value:
                lines.append(f"{label}: {escape(value)}")
        if payload.reason:
            lines.append("")
            lines.append(escape(payload.reason))
        return "\n".join(lines)
