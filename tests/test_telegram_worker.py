"""Delivery queue worker: send/edit, retries, 429, dead letter, rate limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import FakeClock
from tests.fake_telegram import FakeTelegramClient
from tests.test_telegram_queue import GROUP, make_settings, request

from app.config import TelegramSettings
from app.domain.enums import (
    TelegramDeliveryOperation,
    TelegramDeliveryStatus,
    TelegramDeliveryType,
)
from app.repositories.telegram_delivery_repository import TelegramDeliveryRepository
from app.telegram.delivery_queue import DeliveryQueueWorker
from app.telegram.delivery_service import TelegramDeliveryService
from app.telegram.formatter import TelegramFormatter
from app.telegram.rate_limiter import TelegramRateLimiter
from app.telegram.retry import (
    TelegramPermanentError,
    TelegramRateLimitedError,
    TelegramRetryableError,
)


def aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; normalise to UTC for comparisons."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class WorkerHarness:
    def __init__(self, session_factory, settings: TelegramSettings | None = None) -> None:
        self.settings = settings or make_settings(retry_jitter_seconds=0.0, max_retry_attempts=3)
        self.repo = TelegramDeliveryRepository(session_factory)
        self.service = TelegramDeliveryService(self.repo, self.settings)
        self.client = FakeTelegramClient()
        self.clock = FakeClock()
        self.now = {"value": datetime(2026, 8, 21, 12, 0, tzinfo=UTC)}
        self.sleeps: list[float] = []
        self.dead_letters: list[str] = []

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.clock.advance(seconds)

        async def on_dead_letter(row) -> None:
            self.dead_letters.append(row.message_type)

        self.worker = DeliveryQueueWorker(
            self.repo,
            self.client,
            TelegramFormatter(),
            TelegramRateLimiter(self.settings, clock=self.clock),
            self.settings,
            sleep=sleep,
            rng=lambda: 0.0,
            now_fn=lambda: self.now["value"],
            on_dead_letter=on_dead_letter,
        )

    def advance(self, seconds: float) -> None:
        self.now["value"] += timedelta(seconds=seconds)
        self.clock.advance(seconds)


async def test_successful_send_marks_sent(session_factory) -> None:
    harness = WorkerHarness(session_factory)
    row = await harness.service.enqueue(request("k1"))
    processed = await harness.worker.process_once()
    assert processed == 1
    assert harness.client.sent[0][0] == GROUP
    stored = await harness.repo.get(row.delivery_id)
    assert stored.status == TelegramDeliveryStatus.SENT.value
    assert stored.message_id is not None
    assert stored.sent_at is not None


async def test_edit_operation_edits_existing_message(session_factory) -> None:
    harness = WorkerHarness(session_factory)
    await harness.service.enqueue(
        request("k1", operation=TelegramDeliveryOperation.EDIT, message_id=77)
    )
    await harness.worker.process_once()
    assert harness.client.edited == [(GROUP, 77, harness.client.edited[0][2])]
    counts = await harness.repo.counts_by_status()
    assert counts[TelegramDeliveryStatus.EDITED.value] == 1


async def test_429_reschedules_with_retry_after(session_factory) -> None:
    harness = WorkerHarness(session_factory)
    row = await harness.service.enqueue(request("k1"))
    harness.client.fail_next(TelegramRateLimitedError("429", retry_after=30.0))
    await harness.worker.process_once()
    stored = await harness.repo.get(row.delivery_id)
    assert stored.status == TelegramDeliveryStatus.RETRYING.value
    assert stored.last_error_class == "rate_limited"
    delta = (aware(stored.scheduled_at) - harness.now["value"]).total_seconds()
    assert delta == pytest.approx(30.0)
    # not due yet
    assert await harness.worker.process_once() == 0
    # after retry_after has passed the send succeeds
    harness.advance(31.0)
    await harness.worker.process_once()
    stored = await harness.repo.get(row.delivery_id)
    assert stored.status == TelegramDeliveryStatus.SENT.value
    assert stored.attempt_count == 2


async def test_transient_errors_use_backoff_then_succeed(session_factory) -> None:
    harness = WorkerHarness(session_factory)
    row = await harness.service.enqueue(request("k1"))
    harness.client.fail_next(TelegramRetryableError("transport"))
    await harness.worker.process_once()
    stored = await harness.repo.get(row.delivery_id)
    assert stored.status == TelegramDeliveryStatus.RETRYING.value
    delta = (aware(stored.scheduled_at) - harness.now["value"]).total_seconds()
    assert delta == pytest.approx(1.0)  # retry_min * 2^0
    harness.advance(2.0)
    await harness.worker.process_once()
    assert (await harness.repo.get(row.delivery_id)).status == TelegramDeliveryStatus.SENT.value


async def test_permanent_error_fails_without_retry(session_factory) -> None:
    harness = WorkerHarness(session_factory)
    row = await harness.service.enqueue(request("k1"))
    harness.client.fail_next(TelegramPermanentError("403: chat not found"))
    await harness.worker.process_once()
    stored = await harness.repo.get(row.delivery_id)
    assert stored.status == TelegramDeliveryStatus.FAILED.value
    assert stored.last_error_class == "permanent"
    assert await harness.worker.process_once() == 0  # never retried


async def test_exhausted_retries_become_dead_letter(session_factory) -> None:
    harness = WorkerHarness(session_factory)
    row = await harness.service.enqueue(request("k1"))
    for _ in range(3):  # max_retry_attempts = 3
        harness.client.fail_next(TelegramRetryableError("transport"))
        harness.advance(120.0)
        await harness.worker.process_once()
    stored = await harness.repo.get(row.delivery_id)
    assert stored.status == TelegramDeliveryStatus.DEAD_LETTER.value
    assert stored.attempt_count == 3
    assert harness.dead_letters == [TelegramDeliveryType.SYSTEM_WARNING.value]


async def test_worker_waits_for_rate_limit_slot(session_factory) -> None:
    harness = WorkerHarness(session_factory)
    await harness.service.enqueue(request("k1", title="a", dedup=False))
    await harness.service.enqueue(request("k2", title="b", dedup=False))
    await harness.worker.process_once()
    assert len(harness.client.sent) == 2
    # second message had to wait for the min interval
    assert any(delay >= 1.0 for delay in harness.sleeps)


async def test_malformed_payload_fails_safely(session_factory) -> None:
    harness = WorkerHarness(session_factory)
    row = await harness.service.enqueue(request("k1"))
    # corrupt the stored payload directly
    import sqlalchemy as sa

    from app.repositories.orm import TelegramDelivery

    async with session_factory() as session:
        await session.execute(
            sa.update(TelegramDelivery)
            .where(TelegramDelivery.delivery_id == row.delivery_id)
            .values(payload={"kind": "unknown"})
        )
        await session.commit()
    await harness.worker.process_once()
    stored = await harness.repo.get(row.delivery_id)
    assert stored.status == TelegramDeliveryStatus.FAILED.value
    assert stored.last_error_class == "malformed_payload"


async def test_auth_error_sets_auth_failed_flag(session_factory) -> None:
    from app.telegram.retry import TelegramAuthError

    harness = WorkerHarness(session_factory)
    await harness.service.enqueue(request("k1"))
    harness.client.fail_next(TelegramAuthError("401"))
    await harness.worker.process_once()
    assert harness.worker.auth_failed is True


async def test_worker_lifecycle_start_stop(session_factory) -> None:
    import asyncio

    harness = WorkerHarness(session_factory)
    await harness.worker.start()
    assert harness.worker.alive
    await harness.service.enqueue(request("k1"))
    harness.worker.wake()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if harness.client.sent:
            break
    assert harness.client.sent
    await harness.worker.stop()
    assert not harness.worker.alive
