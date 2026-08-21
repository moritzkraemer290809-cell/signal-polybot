"""Delivery persistence: idempotency, dedup, priorities, overflow, leases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import TelegramSettings
from app.domain.enums import (
    TelegramDeliveryOperation,
    TelegramDeliveryStatus,
    TelegramDeliveryType,
)
from app.repositories.telegram_delivery_repository import TelegramDeliveryRepository
from app.telegram.delivery_service import TelegramDeliveryService
from app.telegram.models import DeliveryRequest, SystemMessagePayload

GROUP = -100_555


def make_settings(**overrides) -> TelegramSettings:
    defaults = dict(enabled=True, bot_token="test-token", group_id=GROUP)
    defaults.update(overrides)
    return TelegramSettings(_env_file=None, **defaults)


def request(
    key: str,
    delivery_type: TelegramDeliveryType = TelegramDeliveryType.SYSTEM_WARNING,
    title: str = "hinweis",
    **kwargs,
) -> DeliveryRequest:
    return DeliveryRequest(
        delivery_type=delivery_type,
        payload=SystemMessagePayload(title=title, fields={}),
        idempotency_key=key,
        **kwargs,
    )


@pytest.fixture
def repo(session_factory) -> TelegramDeliveryRepository:
    return TelegramDeliveryRepository(session_factory)


@pytest.fixture
def service(repo) -> TelegramDeliveryService:
    return TelegramDeliveryService(repo, make_settings())


async def test_enqueue_persists_pending_delivery(service, repo) -> None:
    row = await service.enqueue(request("k1"))
    assert row is not None
    assert row.status == TelegramDeliveryStatus.PENDING.value
    assert row.chat_id == GROUP
    assert row.priority == 2
    assert await repo.open_count() == 1


async def test_idempotency_key_is_unique_across_instances(service, repo, session_factory) -> None:
    first = await service.enqueue(request("k1"))
    # a "restarted" process: fresh service + repository over the same database
    service2 = TelegramDeliveryService(TelegramDeliveryRepository(session_factory), make_settings())
    second = await service2.enqueue(request("k1", title="hinweis"))
    assert second is not None and first is not None
    assert second.delivery_id == first.delivery_id
    assert await repo.open_count() == 1


async def test_sent_delivery_is_never_resent(service, repo) -> None:
    row = await service.enqueue(request("k1"))
    await repo.mark_sent(row.delivery_id, message_id=42)
    again = await service.enqueue(request("k1"))
    assert again.status == TelegramDeliveryStatus.SENT.value
    assert await repo.claim_due(10, 60.0) == []


async def test_dedup_window_skips_identical_content(service, repo) -> None:
    await service.enqueue(request("k1", title="gleicher inhalt"))
    duplicate = await service.enqueue(request("k2", title="gleicher inhalt"))
    assert duplicate.status == TelegramDeliveryStatus.SKIPPED_DUPLICATE.value
    different = await service.enqueue(request("k3", title="anderer inhalt"))
    assert different.status == TelegramDeliveryStatus.PENDING.value


async def test_dedup_can_be_disabled_per_request(service) -> None:
    await service.enqueue(request("k1", title="x"))
    row = await service.enqueue(request("k2", title="x", dedup=False))
    assert row.status == TelegramDeliveryStatus.PENDING.value


async def test_edit_requires_message_id(service) -> None:
    with pytest.raises(ValueError):
        await service.enqueue(request("k1", operation=TelegramDeliveryOperation.EDIT))
    row = await service.enqueue(
        request("k2", operation=TelegramDeliveryOperation.EDIT, message_id=7)
    )
    assert row.operation == TelegramDeliveryOperation.EDIT.value


async def test_claim_orders_by_priority(service, repo) -> None:
    await service.enqueue(request("low", TelegramDeliveryType.WATCHLIST))
    await service.enqueue(request("normal", TelegramDeliveryType.SIGNAL_UPDATE))
    await service.enqueue(request("critical", TelegramDeliveryType.SYSTEM_ERROR))
    batch = await repo.claim_due(10, 60.0)
    assert [row.idempotency_key for row in batch] == ["critical", "normal", "low"]
    assert all(row.status == TelegramDeliveryStatus.PROCESSING.value for row in batch)
    assert all(row.attempt_count == 1 for row in batch)


async def test_scheduled_deliveries_wait_until_due(service, repo) -> None:
    future = datetime.now(tz=UTC) + timedelta(minutes=5)
    await service.enqueue(request("later", scheduled_at=future))
    assert await repo.claim_due(10, 60.0) == []
    batch = await repo.claim_due(10, 60.0, now=future + timedelta(seconds=1))
    assert len(batch) == 1


async def test_lease_recovery_reclaims_stuck_processing(service, repo) -> None:
    row = await service.enqueue(request("stuck"))
    now = datetime.now(tz=UTC)
    claimed = await repo.claim_due(10, lease_seconds=60.0, now=now)
    assert len(claimed) == 1
    # not yet expired: a second claim gets nothing
    assert await repo.claim_due(10, 60.0, now=now + timedelta(seconds=30)) == []
    # expired lease: the delivery is reclaimed and re-attempted
    reclaimed = await repo.claim_due(10, 60.0, now=now + timedelta(seconds=120))
    assert [r.delivery_id for r in reclaimed] == [row.delivery_id]
    assert reclaimed[0].attempt_count == 2


async def test_overflow_drops_low_priority_first(session_factory) -> None:
    repo = TelegramDeliveryRepository(session_factory)
    service = TelegramDeliveryService(repo, make_settings(delivery_queue_size=2))
    await service.enqueue(request("w1", TelegramDeliveryType.WATCHLIST, title="a"))
    await service.enqueue(request("w2", TelegramDeliveryType.DAILY_STATUS, title="b"))
    critical = await service.enqueue(request("crit", TelegramDeliveryType.SYSTEM_ERROR, title="c"))
    assert critical.status == TelegramDeliveryStatus.PENDING.value
    counts = await repo.counts_by_status()
    assert counts.get(TelegramDeliveryStatus.CANCELLED.value) == 1


async def test_overflow_rejects_incoming_low_priority_when_full_of_critical(
    session_factory,
) -> None:
    repo = TelegramDeliveryRepository(session_factory)
    service = TelegramDeliveryService(repo, make_settings(delivery_queue_size=2))
    await service.enqueue(request("c1", TelegramDeliveryType.SYSTEM_ERROR, title="a"))
    await service.enqueue(request("c2", TelegramDeliveryType.SIGNAL_STOP, title="b"))
    low = await service.enqueue(request("w1", TelegramDeliveryType.WATCHLIST, title="c"))
    assert low.status == TelegramDeliveryStatus.CANCELLED.value
    # critical beyond the cap is still accepted - never rejected
    extra = await service.enqueue(request("c3", TelegramDeliveryType.SIGNAL_INVALIDATED, title="d"))
    assert extra.status == TelegramDeliveryStatus.PENDING.value


async def test_pause_gates_low_priority_only(session_factory) -> None:
    repo = TelegramDeliveryRepository(session_factory)
    paused = {"value": True}

    async def is_paused() -> bool:
        return paused["value"]

    service = TelegramDeliveryService(repo, make_settings(), is_paused=is_paused)
    gated = await service.enqueue(request("w", TelegramDeliveryType.WATCHLIST))
    assert gated is None
    critical = await service.enqueue(request("e", TelegramDeliveryType.SYSTEM_ERROR))
    assert critical is not None
    paused_notice = await service.enqueue(request("p", TelegramDeliveryType.BOT_PAUSED))
    assert paused_notice is not None


async def test_status_counts_and_dead_letters_visible(service, repo) -> None:
    row = await service.enqueue(request("k1"))
    await repo.mark_dead_letter(row.delivery_id, "rate_limited")
    assert await repo.dead_letter_count() == 1
    counts = await repo.counts_by_status()
    assert counts[TelegramDeliveryStatus.DEAD_LETTER.value] == 1
