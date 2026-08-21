"""Admin authorization for Telegram commands.

Only user ids from ``TELEGRAM_ADMIN_USER_IDS`` may execute commands, and only
from the configured group or a private chat.  Unauthorized attempts get no
reply (anti-spam), are counted, and are audited without unnecessary personal
data (user id + chat type + command only).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.config import TelegramSettings
from app.observability.logging import get_logger
from app.observability.metrics import metrics

AuditFn = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class TelegramAuthorizer:
    def __init__(self, settings: TelegramSettings, audit: AuditFn | None = None) -> None:
        self._settings = settings
        self._audit = audit
        self._log = get_logger("telegram_auth")

    def _chat_allowed(self, chat_id: int, chat_type: str) -> bool:
        if chat_type == "private":
            return True
        return self._settings.group_id is not None and chat_id == self._settings.group_id

    def is_authorized(self, user_id: int, chat_id: int, chat_type: str) -> bool:
        return self._settings.is_admin(user_id) and self._chat_allowed(chat_id, chat_type)

    async def reject(self, user_id: int, chat_type: str, command: str) -> None:
        metrics.increment("telegram.command_unauthorized")
        self._log.warning(
            "unauthorized_command",
            user_id=user_id,
            chat_type=chat_type,
            command=command,
        )
        if self._audit is not None and self._settings.admin_command_audit_enabled:
            await self._audit(
                "telegram_unauthorized_command",
                f"unauthorized telegram command {command}",
                {"user_id": user_id, "chat_type": chat_type, "command": command},
            )
