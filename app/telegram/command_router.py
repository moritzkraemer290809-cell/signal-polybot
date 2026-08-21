"""Admin command parsing, cooldowns and handlers.

The router parses updates, enforces authorization and per-admin cooldowns and
dispatches to handlers.  Handlers contain no direct database logic - they use
injected services/providers and return plain reply text (HTML-escaped by the
formatter helpers).  No command can trigger trading or order execution: no
such code path exists anywhere in this project.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.bot_state import BotStateService
from app.config import TelegramSettings
from app.domain.enums import TelegramDeliveryType
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.telegram.authorization import AuditFn, TelegramAuthorizer
from app.telegram.delivery_service import TelegramDeliveryService
from app.telegram.formatter import escape
from app.telegram.idempotency import bot_state_key
from app.telegram.models import DailyStatusPayload, DeliveryRequest, SystemMessagePayload

#: read-only commands subject to the status cooldown
_COOLDOWN_COMMANDS = {"status", "health", "daily"}


@dataclass(frozen=True)
class ParsedCommand:
    command: str
    user_id: int
    chat_id: int
    chat_type: str
    message_ts: int


@dataclass
class CommandProviders:
    """Async snapshot providers wired in bootstrap (no DB logic in handlers)."""

    status_snapshot: Callable[[], Awaitable[dict[str, Any]]]
    health_snapshot: Callable[[], Awaitable[dict[str, Any]]]
    daily_snapshot: Callable[[], Awaitable[DailyStatusPayload]]


@dataclass
class _Cooldowns:
    seconds: float
    clock: Callable[[], float] = time.monotonic
    last_use: dict[tuple[int, str], float] = field(default_factory=dict)

    def check_and_record(self, user_id: int, command: str) -> bool:
        if command not in _COOLDOWN_COMMANDS or self.seconds <= 0:
            return True
        key = (user_id, command)
        now = self.clock()
        last = self.last_use.get(key)
        if last is not None and now - last < self.seconds:
            return False
        self.last_use[key] = now
        return True


def parse_update(update: dict[str, Any], bot_username: str | None) -> ParsedCommand | None:
    """Extract a command from a Telegram update; None for anything else."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    command_token = text.split()[0][1:]
    if "@" in command_token:
        command_token, _, target = command_token.partition("@")
        if bot_username and target.lower() != bot_username.lower():
            return None  # addressed to another bot
    try:
        return ParsedCommand(
            command=command_token.lower(),
            user_id=int(sender["id"]),
            chat_id=int(chat["id"]),
            chat_type=str(chat.get("type", "")),
            message_ts=int(message.get("date", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


class TelegramCommandRouter:
    def __init__(
        self,
        settings: TelegramSettings,
        authorizer: TelegramAuthorizer,
        bot_state: BotStateService,
        delivery_service: TelegramDeliveryService,
        providers: CommandProviders,
        *,
        bot_username: str | None = None,
        audit: AuditFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._authorizer = authorizer
        self._bot_state = bot_state
        self._delivery = delivery_service
        self._providers = providers
        self.bot_username = bot_username
        self._audit = audit
        self._cooldowns = _Cooldowns(settings.status_command_cooldown_seconds, clock)
        self._log = get_logger("telegram_commands")

    async def handle_update(self, update: dict[str, Any]) -> tuple[int, str] | None:
        """Process one update.  Returns (chat_id, reply_text) or None."""
        parsed = parse_update(update, self.bot_username)
        if parsed is None:
            return None
        if not self._authorizer.is_authorized(parsed.user_id, parsed.chat_id, parsed.chat_type):
            await self._authorizer.reject(parsed.user_id, parsed.chat_type, parsed.command)
            return None
        handler = getattr(self, f"_cmd_{parsed.command}", None)
        if handler is None:
            return None
        if not self._cooldowns.check_and_record(parsed.user_id, parsed.command):
            metrics.increment("telegram.command_cooldown_rejected")
            return None
        metrics.increment("telegram.command_authorized")
        self._log.info("admin_command", command=parsed.command, user_id=parsed.user_id)
        reply: str | None = await handler(parsed)
        if reply is None:
            return None
        return parsed.chat_id, reply

    # ------------------------------------------------------------- commands

    async def _cmd_status(self, parsed: ParsedCommand) -> str:
        snapshot = await self._providers.status_snapshot()
        lines = ["<b>Status</b>", ""]
        for label, value in snapshot.items():
            lines.append(f"{escape(label)}: {escape(value)}")
        return "\n".join(lines)

    async def _cmd_health(self, parsed: ParsedCommand) -> str:
        snapshot = await self._providers.health_snapshot()
        overall = snapshot.pop("overall", "UNKNOWN")
        lines = [f"<b>Health: {escape(overall)}</b>", ""]
        for label, value in snapshot.items():
            lines.append(f"{escape(label)}: {escape(value)}")
        return "\n".join(lines)

    async def _cmd_pause(self, parsed: ParsedCommand) -> str:
        changed = await self._bot_state.set_paused(True, changed_by=parsed.user_id)
        if self._audit is not None and self._settings.admin_command_audit_enabled:
            await self._audit(
                "telegram_admin_pause",
                "bot paused via telegram command",
                {"user_id": parsed.user_id, "changed": changed},
            )
        if changed:
            await self._delivery.enqueue(
                DeliveryRequest(
                    delivery_type=TelegramDeliveryType.BOT_PAUSED,
                    payload=SystemMessagePayload(
                        title="Bot pausiert",
                        fields={
                            "Neue Signale": "gestoppt",
                            "Data Engine": "läuft weiter",
                            "Kritische Warnungen": "weiterhin aktiv",
                        },
                    ),
                    idempotency_key=bot_state_key("pause", self._bot_state.change_counter),
                )
            )
            return "⏸ Bot pausiert."
        return "Bot ist bereits pausiert."

    async def _cmd_resume(self, parsed: ParsedCommand) -> str:
        changed = await self._bot_state.set_paused(False, changed_by=parsed.user_id)
        if self._audit is not None and self._settings.admin_command_audit_enabled:
            await self._audit(
                "telegram_admin_resume",
                "bot resumed via telegram command",
                {"user_id": parsed.user_id, "changed": changed},
            )
        if changed:
            await self._delivery.enqueue(
                DeliveryRequest(
                    delivery_type=TelegramDeliveryType.BOT_RESUMED,
                    payload=SystemMessagePayload(title="Bot fortgesetzt", fields={}),
                    idempotency_key=bot_state_key("resume", self._bot_state.change_counter),
                )
            )
            return "▶️ Bot fortgesetzt."
        return "Bot ist bereits aktiv."

    async def _cmd_open(self, parsed: ParsedCommand) -> str:
        return "Keine offenen Signale. Signal-Lifecycle wird in einer späteren Phase aktiviert."

    async def _cmd_watchlist(self, parsed: ParsedCommand) -> str:
        return (
            "Keine Watchlist-Signale verfügbar. "
            "Marktselektion und Strategie folgen in späteren Phasen."
        )

    async def _cmd_daily(self, parsed: ParsedCommand) -> str:
        payload = await self._providers.daily_snapshot()
        hours, rem = divmod(int(payload.uptime_seconds), 3600)
        lines = [
            "<b>Tagesstatus</b>",
            "",
            f"Betriebszeit: {hours}h {rem // 60:02d}m",
            f"WebSocket-Reconnects: {payload.ws_reconnects}",
            f"Verworfene Datenereignisse: {payload.invalid_events}",
        ]
        if payload.quality_counts:
            lines.append(
                "Datenqualität: "
                + " · ".join(f"{escape(k)}: {v}" for k, v in sorted(payload.quality_counts.items()))
            )
        if payload.delivery_counts:
            lines.append(
                "Deliveries: "
                + " · ".join(
                    f"{escape(k)}: {v}" for k, v in sorted(payload.delivery_counts.items())
                )
            )
        lines.append("")
        lines.append("Technischer Status - keine Performance- oder Trading-Aussage.")
        return "\n".join(lines)
