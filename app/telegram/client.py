"""Central Telegram Bot API client (thin, httpx-based).

Design note: instead of a bot framework (aiogram / python-telegram-bot) this
project uses a deliberately thin HTTP client behind the :class:`TelegramClient`
protocol.  Rate limiting, retries, idempotency and command routing are owned
by our own tested layers, so a framework would only wrap three API methods
while adding background machinery we would have to fight.  The protocol keeps
the transport swappable.

Security: the bot token appears only in the request URL built here; it is
never logged, never included in exceptions, and error objects carry only an
error class + sanitized description.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.config import TelegramSettings
from app.observability.logging import get_logger
from app.telegram.retry import (
    TelegramAuthError,
    TelegramPermanentError,
    TelegramRateLimitedError,
    TelegramRetryableError,
)


class TelegramClient(Protocol):
    async def send_message(self, chat_id: int, text: str, parse_mode: str) -> int:
        """Send a message; returns the Telegram message_id."""
        ...

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, parse_mode: str
    ) -> None: ...

    async def get_updates(
        self, offset: int | None, timeout_seconds: float
    ) -> list[dict[str, Any]]: ...

    async def get_me(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class HttpTelegramClient:
    def __init__(self, settings: TelegramSettings, client: httpx.AsyncClient | None = None) -> None:
        if settings.bot_token is None:
            raise ValueError("bot token missing")
        self._token = settings.bot_token.get_secret_value()
        self._base = settings.api_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=settings.polling_timeout_seconds + 10)
        self._owns_client = client is None
        self._log = get_logger("telegram_client")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            response = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise TelegramRetryableError(f"transport: {type(exc).__name__}") from None

        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200 and body.get("ok"):
            return body.get("result")

        description = _sanitize(str(body.get("description", ""))[:120])
        code = body.get("error_code", response.status_code)
        if code == 429:
            retry_after = float((body.get("parameters") or {}).get("retry_after", 5))
            raise TelegramRateLimitedError(description, retry_after=retry_after)
        if code == 401:
            raise TelegramAuthError("unauthorized (invalid bot token)")
        if code in (400, 403, 404):
            raise TelegramPermanentError(f"{code}: {description}")
        if 500 <= int(code) < 600:
            raise TelegramRetryableError(f"{code}: {description}")
        raise TelegramPermanentError(f"{code}: {description}")

    async def send_message(self, chat_id: int, text: str, parse_mode: str) -> int:
        result = await self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
        )
        return int(result["message_id"])

    async def edit_message(self, chat_id: int, message_id: int, text: str, parse_mode: str) -> None:
        await self._call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
        )

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": int(timeout_seconds),
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload)
        return list(result or [])

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        return dict(result or {})


def _sanitize(text: str) -> str:
    """Strip anything that could leak configuration from error descriptions."""
    lowered = text.lower()
    if "token" in lowered or "http" in lowered:
        return "sanitized"
    return text
