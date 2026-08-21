"""Subsystem lifecycle: disabled, misconfigured, polling, startup idempotency."""

from __future__ import annotations

import asyncio

from tests.fake_telegram import FakeTelegramClient, command_update
from tests.test_telegram_commands import RouterHarness
from tests.test_telegram_queue import make_settings

from app.adapters.telegram import TelegramPollingService
from app.domain.enums import (
    TelegramDeliveryType,
    TelegramSubsystemState,
)
from app.repositories.telegram_delivery_repository import TelegramDeliveryRepository
from app.telegram.delivery_service import TelegramDeliveryService
from app.telegram.idempotency import startup_key
from app.telegram.models import DeliveryRequest, SystemMessagePayload
from app.telegram.rate_limiter import TelegramRateLimiter
from app.telegram.subsystem import TelegramSubsystem


def test_disabled_subsystem_reports_disabled_and_builds_nothing() -> None:
    from app.config import TelegramSettings

    settings = TelegramSettings(_env_file=None)  # enabled=False default
    subsystem = TelegramSubsystem(settings=settings)
    assert subsystem.state is TelegramSubsystemState.DISABLED
    assert subsystem.client is None
    assert subsystem.worker is None
    assert subsystem.polling is None


def test_enabled_but_unconfigured_is_degraded() -> None:
    from app.config import TelegramSettings

    settings = TelegramSettings(_env_file=None, enabled=True)  # no token/group
    assert settings.configured is False
    subsystem = TelegramSubsystem(settings=settings)
    assert subsystem.state is TelegramSubsystemState.DEGRADED


async def test_startup_message_is_idempotent_per_correlation_id(session_factory) -> None:
    repo = TelegramDeliveryRepository(session_factory)
    service = TelegramDeliveryService(repo, make_settings())
    correlation = "boot-abc123"
    request = DeliveryRequest(
        delivery_type=TelegramDeliveryType.SYSTEM_STARTUP,
        payload=SystemMessagePayload(title="", fields={"Modus": "Read-only"}),
        idempotency_key=startup_key(correlation),
        correlation_id=correlation,
    )
    first = await service.enqueue(request)
    second = await service.enqueue(request)  # e.g. lifespan retried
    assert first.delivery_id == second.delivery_id
    assert await repo.open_count() == 1


async def test_polling_dispatches_commands_and_sends_reply(session_factory) -> None:
    harness = RouterHarness(session_factory)
    client = FakeTelegramClient()
    polling = TelegramPollingService(
        client,
        harness.router,
        TelegramRateLimiter(harness.settings),
        harness.settings,
        wall_clock=lambda: 0.0,
    )
    client.queue_updates([command_update("/open", chat_id=harness.settings.group_id)])
    await polling.start()
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if client.sent:
                break
        assert client.sent
        assert "Keine offenen Signale" in client.sent[0][1]
        assert polling.updates_processed == 1
        # offset advanced past the consumed update
        assert client.get_updates_calls[-1] == 2
    finally:
        await polling.stop()
    assert not polling.alive


async def test_polling_skips_stale_backlog_after_restart(session_factory) -> None:
    harness = RouterHarness(session_factory)
    client = FakeTelegramClient()
    polling = TelegramPollingService(
        client,
        harness.router,
        TelegramRateLimiter(harness.settings),
        harness.settings,
        wall_clock=lambda: 2_000_000_000.0,
    )
    stale = command_update("/pause", chat_id=harness.settings.group_id, date=1_000_000, update_id=5)
    client.queue_updates([stale])
    await polling.start()
    try:
        await asyncio.sleep(0.1)
        assert await harness.bot_state.is_paused() is False  # not executed
        assert client.get_updates_calls[-1] == 6  # but confirmed via offset
    finally:
        await polling.stop()


async def test_polling_survives_transient_errors(session_factory) -> None:
    from app.telegram.retry import TelegramRetryableError

    harness = RouterHarness(session_factory)
    client = FakeTelegramClient()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)

    polling = TelegramPollingService(
        client,
        harness.router,
        TelegramRateLimiter(harness.settings),
        harness.settings,
        sleep=fake_sleep,
        wall_clock=lambda: 0.0,
    )
    client.fail_next(TelegramRetryableError("transport"))
    client.queue_updates([command_update("/open", chat_id=harness.settings.group_id)])
    await polling.start()
    try:
        for _ in range(200):
            await asyncio.sleep(0.01)
            if client.sent:
                break
        assert client.sent  # recovered after the error
        assert sleeps and sleeps[0] == 5.0
    finally:
        await polling.stop()


async def test_polling_stops_on_auth_error(session_factory) -> None:
    from app.telegram.retry import TelegramAuthError

    harness = RouterHarness(session_factory)
    client = FakeTelegramClient()
    polling = TelegramPollingService(
        client,
        harness.router,
        TelegramRateLimiter(harness.settings),
        harness.settings,
        wall_clock=lambda: 0.0,
    )
    client.fail_next(TelegramAuthError("401"))
    await polling.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if polling.auth_failed:
                break
        assert polling.auth_failed is True
        assert not polling.alive  # loop ended, no retry storm on a bad token
    finally:
        await polling.stop()


async def test_subsystem_health_stats_have_no_secrets(session_factory) -> None:
    import json

    repo = TelegramDeliveryRepository(session_factory)
    settings = make_settings()
    subsystem = TelegramSubsystem(settings=settings, repository=repo, configured=True)
    stats = json.dumps(await subsystem.status_stats())
    assert "test-token" not in stats
    assert str(settings.group_id) not in stats


async def test_worker_dead_marks_degraded(session_factory) -> None:
    from app.telegram.delivery_queue import DeliveryQueueWorker
    from app.telegram.formatter import TelegramFormatter

    settings = make_settings(commands_enabled=False)
    repo = TelegramDeliveryRepository(session_factory)
    client = FakeTelegramClient()
    worker = DeliveryQueueWorker(
        repo, client, TelegramFormatter(), TelegramRateLimiter(settings), settings
    )
    subsystem = TelegramSubsystem(
        settings=settings, repository=repo, client=client, worker=worker, configured=True
    )
    assert subsystem.state is TelegramSubsystemState.DEGRADED  # worker not started
    await subsystem.start()
    assert subsystem.state is TelegramSubsystemState.HEALTHY
    await subsystem.stop()
    assert subsystem.state is TelegramSubsystemState.DEGRADED
    assert client.closed is True


async def test_auth_failure_marks_unavailable(session_factory) -> None:
    from app.telegram.delivery_queue import DeliveryQueueWorker
    from app.telegram.formatter import TelegramFormatter

    settings = make_settings(commands_enabled=False)
    repo = TelegramDeliveryRepository(session_factory)
    client = FakeTelegramClient()
    worker = DeliveryQueueWorker(
        repo, client, TelegramFormatter(), TelegramRateLimiter(settings), settings
    )
    subsystem = TelegramSubsystem(
        settings=settings, repository=repo, client=client, worker=worker, configured=True
    )
    await subsystem.start()
    worker.auth_failed = True
    assert subsystem.state is TelegramSubsystemState.UNAVAILABLE
    await subsystem.stop()
