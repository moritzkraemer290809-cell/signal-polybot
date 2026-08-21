"""Selection coordinator: cycles, locking, transitions, decision history."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from tests.test_eligibility import inputs
from tests.test_market_quality import snapshot
from tests.test_sessions_equity import make_manager
from tests.test_watchlist_service import RecordingNotifier, seed_instruments

from app.config import MarketSelectionSettings
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.market_selection_repository import MarketSelectionRepository
from app.repositories.session_event_repository import SessionEventRepository
from app.repositories.watchlist_repository import WatchlistRepository
from app.selection.enums import SelectionSubsystemState
from app.selection.selection_scheduler import MarketSelectionCoordinator
from app.selection.watchlist_service import WatchlistService
from app.sessions.session_manager import CryptoSessionManager


class StubSelectionService:
    """Duck-typed MarketSelectionService: deterministic inputs per symbol."""

    def __init__(self) -> None:
        self.eligible_symbols: set[str] = set()
        self.build_calls = 0

    def build_inputs(self, row, equity, crypto):
        self.build_calls += 1
        market = snapshot() if row.symbol in self.eligible_symbols else snapshot(spread_bps=99.0)
        return inputs(
            instrument_pk=row.id,
            instrument_id=row.instrument_id,
            symbol=row.symbol,
            market=market,
        )


def make_coordinator(session_factory, *, now: dict | None = None, **settings_overrides):
    from app.config import CryptoSessionSettings

    settings = MarketSelectionSettings(_env_file=None, **settings_overrides)
    notifier = RecordingNotifier()
    service = StubSelectionService()
    now = now or {"value": datetime(2026, 8, 21, 15, 0, tzinfo=UTC)}
    coordinator = MarketSelectionCoordinator(
        settings,
        service,
        WatchlistService(WatchlistRepository(session_factory), settings, notifier),
        make_manager(),
        CryptoSessionManager(CryptoSessionSettings(_env_file=None)),
        InstrumentRepository(session_factory),
        MarketSelectionRepository(session_factory),
        SessionEventRepository(session_factory),
        notifier,
        now_fn=lambda: now["value"],
    )
    return coordinator, service, notifier, now


async def test_run_once_evaluates_and_updates_watchlist(session_factory) -> None:
    await seed_instruments(session_factory, [(1, "BTC-PERP"), (2, "AAPL-PERP")])
    coordinator, service, _, _ = make_coordinator(session_factory)
    service.eligible_symbols = {"BTC-PERP"}
    summary = await coordinator.run_once()
    assert summary["evaluated"] == 2
    assert summary["eligible"] == 1
    assert summary["active_watchlist"] == 1
    assert summary["crypto_session"] == "CRYPTO_24_7"
    assert service.build_calls == 2


async def test_decisions_persisted_only_on_outcome_change(session_factory) -> None:
    await seed_instruments(session_factory, [(1, "BTC-PERP")])
    coordinator, service, _, _ = make_coordinator(session_factory)
    service.eligible_symbols = {"BTC-PERP"}
    await coordinator.run_once()
    await coordinator.run_once()
    await coordinator.run_once()
    repo = MarketSelectionRepository(session_factory)
    decisions = await repo.recent_decisions()
    assert len(decisions) == 1  # unchanged outcome -> single history row

    service.eligible_symbols = set()  # becomes ineligible
    await coordinator.run_once()
    decisions = await repo.recent_decisions()
    assert len(decisions) == 2


async def test_session_transitions_are_recorded(session_factory) -> None:
    await seed_instruments(session_factory, [(1, "BTC-PERP")])
    now = {"value": datetime(2026, 8, 21, 15, 0, tzinfo=UTC)}  # Friday regular
    coordinator, _, _, now_ref = make_coordinator(session_factory, now=now)
    await coordinator.run_once()
    now_ref["value"] = datetime(2026, 8, 22, 15, 0, tzinfo=UTC)  # Saturday
    await coordinator.run_once()
    repo = SessionEventRepository(session_factory)
    events = await repo.recent()
    assert [(event.scope, event.to_state) for event in events] == [("EQUITY", "EQUITY_WEEKEND")]
    assert events[0].from_state == "EQUITY_REGULAR"


async def test_overlapping_runs_are_skipped(session_factory) -> None:
    await seed_instruments(session_factory, [(1, "BTC-PERP")])
    coordinator, _, _, _ = make_coordinator(session_factory)
    async with coordinator._lock:
        summary = await coordinator.run_once()
    assert summary == {}  # skipped: nothing computed yet


async def test_trigger_wakes_the_loop(session_factory) -> None:
    await seed_instruments(session_factory, [(1, "BTC-PERP")])
    coordinator, _, _, now = make_coordinator(session_factory, refresh_seconds=3600)
    await coordinator.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if coordinator.last_run_at is not None:
                break
        first_run = coordinator.last_run_at
        assert first_run is not None
        now["value"] += timedelta(seconds=5)
        coordinator.trigger()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if coordinator.last_run_at != first_run:
                break
        assert coordinator.last_run_at != first_run  # re-ran despite 1h interval
        assert coordinator.state is SelectionSubsystemState.HEALTHY
    finally:
        await coordinator.stop()
    assert coordinator.state is SelectionSubsystemState.UNAVAILABLE


async def test_no_rest_calls_during_evaluation(session_factory) -> None:
    """The cycle uses cached data only - the service never receives a REST
    client, and evaluation happens purely on provided rows."""
    await seed_instruments(session_factory, [(1, "BTC-PERP")])
    coordinator, service, _, _ = make_coordinator(session_factory)
    await coordinator.run_once()
    assert service.build_calls == 1  # one call per instrument, nothing else


async def test_health_and_status_stats_shape(session_factory) -> None:
    await seed_instruments(session_factory, [(1, "BTC-PERP")])
    coordinator, service, _, _ = make_coordinator(session_factory)
    service.eligible_symbols = {"BTC-PERP"}
    await coordinator.run_once()
    health = await coordinator.health_stats()
    assert health["active_watchlist"] == 1
    assert health["calendar_available"] is True
    assert health["calendar_version"] == "us-equity-2026.1"
    status = await coordinator.status_stats()
    assert status["equity_session"] == "EQUITY_REGULAR"
    assert status["crypto_session"] == "CRYPTO_24_7"
    assert status["config"]["min_quality_score"] == 70
    watchlist = await coordinator.watchlist_details()
    assert [entry["symbol"] for entry in watchlist["active"]] == ["BTC-PERP"]
    dashboard = await coordinator.dashboard_details()
    assert dashboard["recent_decisions"][0]["symbol"] == "BTC-PERP"
    # no trading terminology anywhere in selection outputs
    import json

    blob = json.dumps([health, status, watchlist, dashboard]).upper()
    for term in ("LONG", "SHORT", "ENTRY", '"STOP"', "TARGET", "LEVERAGE"):
        assert term not in blob
