"""Telegram long-polling adapter (inbound I/O boundary).

Owns the ``getUpdates`` loop: fetches updates via the central client, hands
them to the command router and sends command replies directly (rate-limited).
Command replies are intentionally NOT persisted deliveries - they are
ephemeral responses to an explicit admin request; broadcasts (pause/resume
confirmations, system alerts, future signals) always go through the
persistent queue instead.

V1 uses long polling only - no outward-facing webhook endpoint exists.
The polling task never blocks the data engine; it is a plain asyncio task
with bounded error backoff and clean cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from app.config import TelegramSettings
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.telegram.client import TelegramClient
from app.telegram.command_router import TelegramCommandRouter
from app.telegram.rate_limiter import TelegramRateLimiter
from app.telegram.retry import TelegramAuthError, TelegramError

_ERROR_BACKOFF_SECONDS = 5.0
#: updates older than service start minus this grace are confirmed but skipped
_STARTUP_SKEW_SECONDS = 60


class TelegramPollingService:
    def __init__(
        self,
        client: TelegramClient,
        router: TelegramCommandRouter,
        rate_limiter: TelegramRateLimiter,
        settings: TelegramSettings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._router = router
        self._rate_limiter = rate_limiter
        self._settings = settings
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._log = get_logger("telegram_polling")

        self._task: asyncio.Task[None] | None = None
        self._offset: int | None = None
        self._started_ts = 0.0
        self.auth_failed = False
        self.updates_processed = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._started_ts = self._wall_clock()
        self._task = asyncio.create_task(self._run(), name="telegram_polling")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        while True:
            try:
                updates = await self._client.get_updates(
                    self._offset, self._settings.polling_timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except TelegramAuthError:
                self.auth_failed = True
                self._log.error("polling_auth_failed")
                return  # invalid token: polling cannot recover by retrying
            except TelegramError as exc:
                self._log.warning("polling_error", error_class=exc.error_class)
                await self._sleep(_ERROR_BACKOFF_SECONDS)
                continue
            except Exception as exc:
                self._log.warning("polling_error", error_class=type(exc).__name__)
                await self._sleep(_ERROR_BACKOFF_SECONDS)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._offset = update_id + 1
                if self._is_stale(update):
                    continue  # confirmed via offset, but not executed
                await self._handle(update)

    def _is_stale(self, update: dict) -> bool:
        message = update.get("message") or {}
        date = message.get("date")
        return isinstance(date, int) and date < self._started_ts - _STARTUP_SKEW_SECONDS

    async def _handle(self, update: dict) -> None:
        try:
            result = await self._router.handle_update(update)
        except Exception as exc:
            self._log.error("command_handling_failed", error=type(exc).__name__)
            return
        self.updates_processed += 1
        if result is None:
            return
        chat_id, reply = result
        wait = self._rate_limiter.send_wait_seconds(chat_id)
        if wait > 0:
            self._rate_limiter.record_wait(wait)
            metrics.increment("telegram.rate_limit_wait_seconds", wait)
            await self._sleep(wait)
        try:
            await self._client.send_message(chat_id, reply, self._settings.parse_mode)
            self._rate_limiter.record_send(chat_id)
        except Exception as exc:
            # command replies are best effort - never crash polling
            self._log.warning("command_reply_failed", error=type(exc).__name__)
