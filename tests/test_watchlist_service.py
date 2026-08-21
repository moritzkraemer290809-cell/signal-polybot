"""Watchlist lifecycle: add/pause/restore/remove, persistence, resilience."""

from __future__ import annotations

import dataclasses

import pytest
from tests.test_eligibility import evaluate_instrument, inputs
from tests.test_market_quality import snapshot

from app.config import MarketSelectionSettings
from app.repositories.watchlist_repository import WatchlistRepository
from app.selection.enums import InstrumentEligibilityStatus as ES
from app.selection.enums import MarketSelectionState
from app.selection.selection_events import SelectionNotifier
from app.selection.watchlist_service import WatchlistService


class RecordingNotifier(SelectionNotifier):
    def __init__(self) -> None:
        super().__init__(MarketSelectionSettings(_env_file=None), None, None, None)
        self.paused: list[str] = []
        self.restored: list[str] = []

    async def watchlist_paused(self, symbol, status, detail, decision_id) -> None:
        self.paused.append(symbol)

    async def watchlist_restored(self, symbol, decision_id) -> None:
        self.restored.append(symbol)


async def seed_instruments(session_factory, symbols: list[tuple[int, str]]) -> dict[str, int]:
    from app.domain.models import InstrumentMeta
    from app.repositories.instrument_repository import InstrumentRepository

    repo = InstrumentRepository(session_factory)
    await repo.upsert_discovered(
        [
            InstrumentMeta.from_api(
                {"instrument_id": instrument_id, "symbol": symbol, "category": "crypto"}
            )
            for instrument_id, symbol in symbols
        ],
        {symbol for _, symbol in symbols},
    )
    return {row.symbol: row.id for row in await repo.list_all()}


def evaluation(pk: int, symbol: str, *, eligible: bool = True, score: int = 90, **overrides):
    market = snapshot() if eligible else snapshot(spread_bps=99.0)
    the_inputs = inputs(
        instrument_pk=pk, instrument_id=pk, symbol=symbol, market=market, **overrides
    )
    decision = evaluate_instrument(the_inputs)
    if eligible and score != decision.market_quality_score:
        decision = dataclasses.replace(decision, market_quality_score=score)
    return the_inputs, decision


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


def make_service(session_factory, notifier, **settings_overrides) -> WatchlistService:
    return WatchlistService(
        WatchlistRepository(session_factory),
        MarketSelectionSettings(_env_file=None, **settings_overrides),
        notifier,
    )


async def test_add_pause_restore_cycle(session_factory, notifier) -> None:
    pks = await seed_instruments(session_factory, [(1, "BTC-PERP")])
    pk = pks["BTC-PERP"]
    service = make_service(session_factory, notifier)

    final = await service.reconcile([evaluation(pk, "BTC-PERP")])
    assert final[pk].selection_state is MarketSelectionState.WATCHLIST_ACTIVE
    assert service.active_count() == 1

    # ineligible -> paused with notification
    final = await service.reconcile([evaluation(pk, "BTC-PERP", eligible=False)])
    assert final[pk].selection_state is MarketSelectionState.WATCHLIST_PAUSED
    assert notifier.paused == ["BTC-PERP"]
    assert service.paused_count() == 1

    # eligible again -> restored
    final = await service.reconcile([evaluation(pk, "BTC-PERP")])
    assert final[pk].selection_state is MarketSelectionState.WATCHLIST_ACTIVE
    assert notifier.restored == ["BTC-PERP"]

    repo = WatchlistRepository(session_factory)
    events = [event.event_type for event in await repo.recent_events()]
    assert events == ["WATCHLIST_RESTORED", "WATCHLIST_PAUSED", "WATCHLIST_ADDED"]


async def test_idempotent_reconcile_produces_no_extra_events(session_factory, notifier) -> None:
    pks = await seed_instruments(session_factory, [(1, "BTC-PERP")])
    pk = pks["BTC-PERP"]
    service = make_service(session_factory, notifier)
    await service.reconcile([evaluation(pk, "BTC-PERP")])
    await service.reconcile([evaluation(pk, "BTC-PERP")])
    await service.reconcile([evaluation(pk, "BTC-PERP")])
    repo = WatchlistRepository(session_factory)
    events = await repo.recent_events()
    assert len(events) == 1  # only the initial ADDED


async def test_max_size_prioritises_by_quality_score(session_factory, notifier) -> None:
    pks = await seed_instruments(session_factory, [(1, "A-PERP"), (2, "B-PERP"), (3, "C-PERP")])
    service = make_service(session_factory, notifier, max_watchlist_size=2, default_allowlist=[])
    final = await service.reconcile(
        [
            evaluation(pks["A-PERP"], "A-PERP", score=80),
            evaluation(pks["B-PERP"], "B-PERP", score=95),
            evaluation(pks["C-PERP"], "C-PERP", score=90),
        ]
    )
    assert final[pks["B-PERP"]].selection_state is MarketSelectionState.WATCHLIST_ACTIVE
    assert final[pks["C-PERP"]].selection_state is MarketSelectionState.WATCHLIST_ACTIVE
    assert final[pks["A-PERP"]].selection_state is MarketSelectionState.WATCHLIST_ELIGIBLE
    assert any(reason.code == "WATCHLIST_FULL" for reason in final[pks["A-PERP"]].reasons)


async def test_allowlist_restricts_activation(session_factory, notifier) -> None:
    pks = await seed_instruments(session_factory, [(1, "BTC-PERP"), (2, "DOGE-PERP")])
    service = make_service(session_factory, notifier, default_allowlist=["BTC-PERP"])
    evaluations = [
        evaluation(pks["BTC-PERP"], "BTC-PERP"),
        evaluation(pks["DOGE-PERP"], "DOGE-PERP", allowlisted=False),
    ]
    final = await service.reconcile(evaluations)
    assert final[pks["BTC-PERP"]].selection_state is MarketSelectionState.WATCHLIST_ACTIVE
    doge = final[pks["DOGE-PERP"]]
    assert doge.selection_state is MarketSelectionState.WATCHLIST_ELIGIBLE
    assert any(reason.code == "NOT_IN_ALLOWLIST" for reason in doge.reasons)


async def test_restore_from_persistence(session_factory, notifier) -> None:
    pks = await seed_instruments(session_factory, [(1, "BTC-PERP")])
    pk = pks["BTC-PERP"]
    service = make_service(session_factory, notifier)
    await service.reconcile([evaluation(pk, "BTC-PERP")])

    # "restart": a fresh service instance over the same database
    restarted = make_service(session_factory, RecordingNotifier())
    await restarted.restore()
    assert restarted.restored is True
    assert restarted.active_count() == 1
    assert restarted.snapshot() == {"BTC-PERP": {"state": "WATCHLIST_ACTIVE"}}


async def test_removed_when_instrument_leaves_universe(session_factory, notifier) -> None:
    pks = await seed_instruments(session_factory, [(1, "BTC-PERP")])
    pk = pks["BTC-PERP"]
    service = make_service(session_factory, notifier)
    await service.reconcile([evaluation(pk, "BTC-PERP")])
    final = await service.reconcile([])  # instrument no longer evaluated
    assert final == {}
    assert service.active_count() == 0
    repo = WatchlistRepository(session_factory)
    events = [event.event_type for event in await repo.recent_events()]
    assert events[0] == "WATCHLIST_REMOVED"
    assert await repo.load_all() == []


async def test_database_failure_degrades_without_state_change(notifier) -> None:
    class FailingRepo:
        async def load_all(self):
            raise ConnectionError("db down")

        async def upsert_entry(self, **kwargs):
            raise ConnectionError("db down")

        async def add_event(self, **kwargs):
            raise ConnectionError("db down")

        async def remove_entry(self, pk):
            raise ConnectionError("db down")

    service = WatchlistService(FailingRepo(), MarketSelectionSettings(_env_file=None), notifier)
    await service.restore()
    assert service.degraded is True

    final = await service.reconcile([evaluation(1, "BTC-PERP")])
    # unpersisted change is NOT reported as an activation
    assert final[1].selection_state is not MarketSelectionState.WATCHLIST_ACTIVE
    assert service.active_count() == 0
    assert service.degraded is True


async def test_rejected_instruments_only_produce_decisions(session_factory, notifier) -> None:
    pks = await seed_instruments(session_factory, [(1, "BTC-PERP")])
    service = make_service(session_factory, notifier)
    final = await service.reconcile([evaluation(pks["BTC-PERP"], "BTC-PERP", eligible=False)])
    decision = final[pks["BTC-PERP"]]
    assert decision.eligibility_status is ES.SPREAD_TOO_WIDE
    assert decision.selection_state is MarketSelectionState.REJECTED
    repo = WatchlistRepository(session_factory)
    assert await repo.recent_events() == []  # never listed -> no watchlist event


async def test_pause_reason_recorded(session_factory, notifier) -> None:
    pks = await seed_instruments(session_factory, [(1, "BTC-PERP")])
    pk = pks["BTC-PERP"]
    service = make_service(session_factory, notifier)
    await service.reconcile([evaluation(pk, "BTC-PERP")])
    await service.reconcile([evaluation(pk, "BTC-PERP", eligible=False)])
    repo = WatchlistRepository(session_factory)
    rows = await repo.load_all()
    assert rows[0].state == "WATCHLIST_PAUSED"
    codes = [reason["code"] for reason in rows[0].reasons["reasons"]]
    assert "SPREAD_TOO_WIDE" in codes
    assert rows[0].paused_at is not None
