"""Dynamic instrument discovery against a real (sqlite) repository."""

from __future__ import annotations

from typing import Any

from app.config import UniverseSettings
from app.data.instrument_service import InstrumentService
from app.domain.enums import InstrumentStatus
from app.domain.models import InstrumentMeta
from app.repositories.instrument_repository import InstrumentRepository


class FakeSource:
    def __init__(self, payload: list[dict[str, Any]]) -> None:
        self.payload = payload
        self.calls = 0

    async def get_instruments(self, *, use_cache: bool = True) -> list[InstrumentMeta]:
        self.calls += 1
        return [InstrumentMeta.from_api(item) for item in self.payload]


async def test_refresh_discovers_and_enables_configured_universe(
    session_factory, instruments_payload
) -> None:
    repo = InstrumentRepository(session_factory)
    universe = UniverseSettings(
        _env_file=None, equity_symbols=["AAPL-PERP"], crypto_symbols=["BTC-PERP"]
    )
    source = FakeSource(instruments_payload)
    service = InstrumentService(source, repo, universe)

    result = await service.refresh()
    assert result.discovered_total == 3
    assert sorted(result.created) == ["AAPL-PERP", "BTC-PERP", "DOGE-PERP"]
    assert result.enabled_symbols == ["AAPL-PERP", "BTC-PERP"]
    assert result.missing_configured_symbols == []
    assert service.last_refresh_at is not None

    enabled = await repo.list_enabled()
    assert [row.symbol for row in enabled] == ["AAPL-PERP", "BTC-PERP"]
    aapl = await repo.get_by_symbol("AAPL-PERP")
    assert aapl is not None
    assert aapl.asset_class == "EQUITY"
    assert aapl.instrument_id == 2
    doge = await repo.get_by_symbol("DOGE-PERP")
    assert doge is not None and doge.enabled is False


async def test_refresh_is_idempotent_and_marks_delisted(
    session_factory, instruments_payload
) -> None:
    repo = InstrumentRepository(session_factory)
    universe = UniverseSettings(
        _env_file=None, equity_symbols=["AAPL-PERP"], crypto_symbols=["BTC-PERP"]
    )
    source = FakeSource(instruments_payload)
    service = InstrumentService(source, repo, universe)
    await service.refresh()

    # second refresh without DOGE-PERP -> delisted, not duplicated
    source.payload = [item for item in instruments_payload if item["symbol"] != "DOGE-PERP"]
    result = await service.refresh()
    assert result.created == []
    assert sorted(result.updated) == ["AAPL-PERP", "BTC-PERP"]
    assert result.delisted == ["DOGE-PERP"]

    rows = await repo.list_all()
    assert len(rows) == 3
    doge = await repo.get_by_symbol("DOGE-PERP")
    assert doge is not None
    assert doge.status == InstrumentStatus.DELISTED.value
    assert doge.enabled is False


async def test_refresh_reports_missing_configured_symbols(
    session_factory, instruments_payload
) -> None:
    repo = InstrumentRepository(session_factory)
    universe = UniverseSettings(
        _env_file=None, equity_symbols=["TSLA-PERP"], crypto_symbols=["BTC-PERP"]
    )
    service = InstrumentService(FakeSource(instruments_payload), repo, universe)
    result = await service.refresh()
    assert result.missing_configured_symbols == ["TSLA-PERP"]
    assert result.enabled_symbols == ["BTC-PERP"]
