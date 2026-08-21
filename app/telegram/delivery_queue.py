"""Persistent Telegram delivery queue worker.

One worker per process claims due deliveries from the database (priority
first), respects the per-chat rate limiter, sends/edits via the central
client and records every state transition.  Leases make crashed workers
recoverable; retries are bounded; nothing here loops busily.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.config import TelegramSettings
from app.domain.enums import TelegramDeliveryOperation, TelegramDeliveryType
from app.observability.logging import get_logger
from app.observability.metrics import metrics
from app.repositories.orm import TelegramDelivery
from app.repositories.telegram_delivery_repository import TelegramDeliveryRepository
from app.telegram.client import TelegramClient
from app.telegram.formatter import TelegramFormatter
from app.telegram.models import payload_from_json
from app.telegram.rate_limiter import TelegramRateLimiter
from app.telegram.retry import TelegramAuthError, decide_retry


class DeliveryQueueWorker:
    def __init__(
        self,
        repository: TelegramDeliveryRepository,
        client: TelegramClient,
        formatter: TelegramFormatter,
        rate_limiter: TelegramRateLimiter,
        settings: TelegramSettings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        now_fn: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        on_dead_letter: Callable[[TelegramDelivery], Awaitable[None]] | None = None,
    ) -> None:
        self._repository = repository
        self._client = client
        self._formatter = formatter
        self._rate_limiter = rate_limiter
        self._settings = settings
        self._sleep = sleep
        self._rng = rng
        self._now = now_fn
        self._on_dead_letter = on_dead_letter
        self._log = get_logger("telegram_delivery_worker")

        self._task: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self.auth_failed = False
        self.last_processed_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="telegram_delivery_worker")

    async def stop(self) -> None:
        """Stop the loop; in-flight leases simply expire and are reclaimed."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def wake(self) -> None:
        self._wakeup.set()

    # ----------------------------------------------------------------- loop

    async def _run(self) -> None:
        while True:
            try:
                processed = await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # repository/db failures: back off, never crash the process
                self._log.error("delivery_worker_cycle_failed", error=type(exc).__name__)
                processed = 0
                await self._sleep(self._settings.delivery_flush_seconds)
            if processed == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wakeup.wait(), timeout=self._settings.delivery_flush_seconds
                    )
                self._wakeup.clear()

    async def process_once(self) -> int:
        """Claim and process one batch.  Returns the number of processed rows."""
        batch = await self._repository.claim_due(
            self._settings.delivery_batch_size,
            self._settings.delivery_lease_seconds,
            self._now(),
        )
        for row in batch:
            await self._process(row)
        if batch:
            self.last_processed_at = self._now()
        return len(batch)

    async def _process(self, row: TelegramDelivery) -> None:
        started = time.monotonic()
        payload = payload_from_json(row.payload)
        if payload is None:
            await self._repository.mark_failed(row.delivery_id, "malformed_payload")
            metrics.increment("telegram.failed")
            return
        try:
            delivery_type = TelegramDeliveryType(row.message_type)
            text = self._formatter.render(delivery_type, payload)
        except Exception:
            await self._repository.mark_failed(row.delivery_id, "format_error")
            metrics.increment("telegram.failed")
            return

        is_edit = row.operation == TelegramDeliveryOperation.EDIT.value
        wait = (
            self._rate_limiter.edit_wait_seconds(row.chat_id)
            if is_edit
            else self._rate_limiter.send_wait_seconds(row.chat_id)
        )
        if wait > 0:
            self._rate_limiter.record_wait(wait)
            metrics.increment("telegram.rate_limit_wait_seconds", wait)
            await self._sleep(wait)

        try:
            if is_edit:
                assert row.message_id is not None  # validated at enqueue time
                await self._client.edit_message(
                    row.chat_id, row.message_id, text, self._settings.parse_mode
                )
                self._rate_limiter.record_edit(row.chat_id)
                await self._repository.mark_edited(row.delivery_id)
                metrics.increment("telegram.edited")
            else:
                message_id = await self._client.send_message(
                    row.chat_id, text, self._settings.parse_mode
                )
                self._rate_limiter.record_send(row.chat_id)
                await self._repository.mark_sent(row.delivery_id, message_id)
                metrics.increment("telegram.sent")
            self._log.info(
                "delivery_completed",
                delivery_id=str(row.delivery_id),
                delivery_type=row.message_type,
                operation=row.operation,
                attempt=row.attempt_count,
                correlation_id=row.correlation_id,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            await self._handle_failure(row, exc)

    async def _handle_failure(self, row: TelegramDelivery, error: Exception) -> None:
        if isinstance(error, TelegramAuthError):
            self.auth_failed = True
        decision = decide_retry(error, row.attempt_count, self._settings, self._rng)
        self._log.warning(
            "delivery_attempt_failed",
            delivery_id=str(row.delivery_id),
            delivery_type=row.message_type,
            attempt=row.attempt_count,
            error_class=decision.error_class,
            retryable=decision.retryable,
            correlation_id=row.correlation_id,
        )
        if decision.retryable and row.attempt_count < self._settings.max_retry_attempts:
            next_at = self._now() + timedelta(seconds=decision.delay_seconds)
            await self._repository.mark_retrying(row.delivery_id, next_at, decision.error_class)
            metrics.increment("telegram.retried")
            return
        if decision.retryable:
            # retryable error but attempts exhausted -> dead letter
            await self._repository.mark_dead_letter(row.delivery_id, decision.error_class)
            metrics.increment("telegram.dead_letter")
            if self._on_dead_letter is not None:
                with contextlib.suppress(Exception):
                    await self._on_dead_letter(row)
            return
        # permanent error -> failed, never retried
        await self._repository.mark_failed(row.delivery_id, decision.error_class)
        metrics.increment("telegram.failed")
