"""Persistence for discovered instruments."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import InstrumentStatus
from app.domain.models import InstrumentMeta, utc_now
from app.repositories.orm import Instrument


@dataclass
class UniverseUpsertResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    delisted: list[str] = field(default_factory=list)
    enabled_symbols: list[str] = field(default_factory=list)


class InstrumentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert_discovered(
        self,
        discovered: list[InstrumentMeta],
        enabled_symbols: set[str],
        now: datetime | None = None,
    ) -> UniverseUpsertResult:
        """Idempotently sync discovered instruments into the database.

        - new instruments are inserted (``first_seen_at`` set)
        - known instruments are updated (``last_seen_at`` refreshed)
        - previously known instruments missing from discovery are marked DELISTED
        - ``enabled`` reflects membership in the configured universe
        """
        now = now or utc_now()
        result = UniverseUpsertResult()
        seen_ids = {meta.instrument_id for meta in discovered}

        async with self._session_factory() as session:
            rows = (await session.execute(sa.select(Instrument))).scalars().all()
            by_exchange_id = {row.instrument_id: row for row in rows}

            for meta in discovered:
                enabled = meta.symbol in enabled_symbols
                row = by_exchange_id.get(meta.instrument_id)
                if row is None:
                    row = Instrument(
                        instrument_id=meta.instrument_id,
                        symbol=meta.symbol,
                        first_seen_at=now,
                    )
                    session.add(row)
                    result.created.append(meta.symbol)
                else:
                    result.updated.append(meta.symbol)
                row.symbol = meta.symbol
                row.instrument_type = meta.instrument_type
                row.category = meta.category
                row.asset_class = meta.asset_class.value
                row.base_asset = meta.base_asset
                row.quote_asset = meta.quote_asset
                row.price_decimals = meta.price_decimals
                row.quantity_decimals = meta.quantity_decimals
                row.min_notional = meta.min_notional
                row.max_leverage = meta.max_leverage
                row.funding_interval = meta.funding_interval
                row.liquidation_fee = meta.liquidation_fee
                row.isolated_only = meta.isolated_only
                row.risk_tiers = {
                    "tiers": [
                        {"lower_bound": str(tier.lower_bound), "max_leverage": tier.max_leverage}
                        for tier in meta.risk_tiers
                    ]
                }
                row.status = meta.status.value
                row.enabled = enabled
                row.last_seen_at = now
                row.metadata_raw = meta.raw
                if enabled:
                    result.enabled_symbols.append(meta.symbol)

            for exchange_id, row in by_exchange_id.items():
                if exchange_id not in seen_ids and row.status != InstrumentStatus.DELISTED:
                    row.status = InstrumentStatus.DELISTED.value
                    row.enabled = False
                    result.delisted.append(row.symbol)

            await session.commit()
        return result

    async def get_by_symbol(self, symbol: str) -> Instrument | None:
        async with self._session_factory() as session:
            return (
                await session.execute(sa.select(Instrument).where(Instrument.symbol == symbol))
            ).scalar_one_or_none()

    async def list_enabled(self) -> list[Instrument]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(Instrument)
                .where(Instrument.enabled.is_(True))
                .order_by(Instrument.symbol)
            )
            return list(rows.scalars().all())

    async def list_all(self) -> list[Instrument]:
        async with self._session_factory() as session:
            rows = await session.execute(sa.select(Instrument).order_by(Instrument.symbol))
            return list(rows.scalars().all())
