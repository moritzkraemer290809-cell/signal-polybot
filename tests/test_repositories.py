"""Repository behaviour against sqlite (idempotency, counting)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.enums import (
    DataSource,
    SignalStatus,
    SystemEventLevel,
    Timeframe,
)
from app.domain.models import CandleData, InstrumentMeta
from app.repositories.candle_repository import CandleRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.orm import Signal
from app.repositories.signal_repository import SignalRepository
from app.repositories.system_event_repository import SystemEventRepository


async def _seed_instrument(session_factory) -> int:
    repo = InstrumentRepository(session_factory)
    meta = InstrumentMeta.from_api({"instrument_id": 1, "symbol": "BTC-PERP", "category": "crypto"})
    await repo.upsert_discovered([meta], {"BTC-PERP"})
    row = await repo.get_by_symbol("BTC-PERP")
    assert row is not None
    return row.id


def _candle(minute: int, close: str = "101") -> CandleData:
    return CandleData(
        timeframe=Timeframe.M1,
        open_time=datetime(2026, 8, 21, 12, minute, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=Decimal("10"),
        trade_count=5,
    )


async def test_candle_upsert_is_idempotent(session_factory) -> None:
    instrument_pk = await _seed_instrument(session_factory)
    repo = CandleRepository(session_factory)

    await repo.upsert_candles(instrument_pk, [_candle(0), _candle(1)])
    await repo.upsert_candles(instrument_pk, [_candle(0, close="105")], DataSource.REST)

    rows = await repo.get_range(
        instrument_pk,
        Timeframe.M1,
        datetime(2026, 8, 21, tzinfo=UTC),
        datetime(2026, 8, 22, tzinfo=UTC),
    )
    assert len(rows) == 2
    assert str(rows[0].close) in ("105", "105.000000000000000000")
    latest = await repo.latest_open_time(instrument_pk, Timeframe.M1)
    assert latest is not None


async def test_signal_open_count(session_factory) -> None:
    instrument_pk = await _seed_instrument(session_factory)
    repo = SignalRepository(session_factory)
    assert await repo.count_open() == 0

    async with session_factory() as session:
        session.add(
            Signal(
                id=uuid.uuid4(),
                short_id="BTC-L-20260821-0001",
                instrument_pk=instrument_pk,
                symbol="BTC-PERP",
                direction="LONG",
                status=SignalStatus.WATCHING.value,
            )
        )
        session.add(
            Signal(
                id=uuid.uuid4(),
                short_id="BTC-S-20260821-0002",
                instrument_pk=instrument_pk,
                symbol="BTC-PERP",
                direction="SHORT",
                status=SignalStatus.CLOSED_STOP.value,
            )
        )
        await session.commit()

    assert await repo.count_open() == 1
    open_signals = await repo.list_open()
    assert open_signals[0].short_id == "BTC-L-20260821-0001"


async def test_system_events_roundtrip(session_factory) -> None:
    repo = SystemEventRepository(session_factory)
    await repo.add(SystemEventLevel.WARNING, "data_stale", "ticker stale", {"symbol": "BTC-PERP"})
    events = await repo.latest()
    assert len(events) == 1
    assert events[0].level == "WARNING"
    assert events[0].context == {"symbol": "BTC-PERP"}
