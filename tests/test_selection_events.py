"""Selection notifications: defaults off, paused-mode suppression, no
trading terminology, dedup via delivery queue."""

from __future__ import annotations

from tests.test_telegram_queue import make_settings as make_telegram_settings

from app.config import MarketSelectionSettings
from app.domain.enums import TelegramDeliveryStatus, TelegramDeliveryType
from app.repositories.telegram_delivery_repository import TelegramDeliveryRepository
from app.selection.selection_events import SelectionNotifier
from app.telegram.delivery_service import TelegramDeliveryService


class RecordingSystemEvents:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def add(self, level, event_type, message, context=None, correlation_id=None):
        self.events.append(event_type)


class RecordingDelivery:
    def __init__(self) -> None:
        self.requests: list = []

    async def enqueue(self, request):
        self.requests.append(request)
        return object()


class FakeBotState:
    def __init__(self, paused: bool = False) -> None:
        self.paused = paused

    async def is_paused(self) -> bool:
        return self.paused


def make_notifier(*, notify_state=False, notify_session=False, paused=False):
    delivery = RecordingDelivery()
    system_events = RecordingSystemEvents()
    notifier = SelectionNotifier(
        MarketSelectionSettings(
            _env_file=None,
            notify_state_changes=notify_state,
            notify_session_changes=notify_session,
        ),
        system_events,
        delivery,
        FakeBotState(paused),
    )
    return notifier, delivery, system_events


async def test_no_telegram_messages_by_default() -> None:
    notifier, delivery, system_events = make_notifier()
    await notifier.session_transition("equity", "EQUITY_CLOSED", "EQUITY_REGULAR", None)
    await notifier.watchlist_paused("AAPL-PERP", "DATA_STALE", "orderbook stale", "id-1")
    await notifier.watchlist_restored("AAPL-PERP", "id-2")
    await notifier.calendar_unavailable("coverage ended")
    assert delivery.requests == []  # defaults: never notify
    # but system events are always persisted
    assert system_events.events == [
        "session_transition",
        "watchlist_paused",
        "watchlist_restored",
        "calendar_unavailable",
    ]


async def test_optional_notices_on_real_state_change() -> None:
    notifier, delivery, _ = make_notifier(notify_state=True, notify_session=True)
    await notifier.session_transition("equity", "EQUITY_CLOSED", "EQUITY_REGULAR", None)
    await notifier.watchlist_paused("AAPL-PERP", "DATA_STALE", "orderbook stale", "id-1")
    types = [request.delivery_type for request in delivery.requests]
    assert TelegramDeliveryType.SYSTEM_WARNING in types
    assert TelegramDeliveryType.DATA_QUALITY_WARNING in types


async def test_paused_mode_suppresses_all_selection_notices() -> None:
    notifier, delivery, system_events = make_notifier(
        notify_state=True, notify_session=True, paused=True
    )
    await notifier.session_transition("equity", "EQUITY_CLOSED", "EQUITY_REGULAR", None)
    await notifier.watchlist_paused("AAPL-PERP", "DATA_STALE", "detail", "id-1")
    assert delivery.requests == []  # optional notices suppressed while paused
    assert len(system_events.events) == 2  # persistence continues


async def test_notices_contain_no_trading_terms() -> None:
    notifier, delivery, _ = make_notifier(notify_state=True, notify_session=True)
    await notifier.watchlist_paused("AAPL-PERP", "DATA_STALE", "orderbook stale", "id-1")
    await notifier.watchlist_restored("AAPL-PERP", "id-2")
    await notifier.session_transition("equity", None, "EQUITY_REGULAR", None)
    import json

    for request in delivery.requests:
        blob = json.dumps(request.payload.model_dump()).upper()
        for term in ("LONG", "SHORT", "ENTRY", "STOP", "TARGET", "LEVERAGE", "TRADE"):
            assert term not in blob


async def test_notices_deduplicate_via_delivery_queue(session_factory) -> None:
    repo = TelegramDeliveryRepository(session_factory)
    delivery_service = TelegramDeliveryService(repo, make_telegram_settings())
    notifier = SelectionNotifier(
        MarketSelectionSettings(_env_file=None, notify_state_changes=True),
        None,
        delivery_service,
        FakeBotState(False),
    )
    # same decision id twice (e.g. retried cycle) -> one delivery, one duplicate skip
    await notifier.watchlist_paused("AAPL-PERP", "DATA_STALE", "detail", "decision-1")
    await notifier.watchlist_paused("AAPL-PERP", "DATA_STALE", "detail", "decision-1")
    counts = await repo.counts_by_status()
    assert counts.get(TelegramDeliveryStatus.PENDING.value) == 1
    assert TelegramDeliveryStatus.FAILED.value not in counts


async def test_notifier_survives_delivery_failure() -> None:
    class ExplodingDelivery:
        async def enqueue(self, request):
            raise ConnectionError("db down")

    notifier = SelectionNotifier(
        MarketSelectionSettings(_env_file=None, notify_state_changes=True),
        None,
        ExplodingDelivery(),
        FakeBotState(False),
    )
    await notifier.watchlist_paused("AAPL-PERP", "DATA_STALE", "detail", "id")  # no raise
