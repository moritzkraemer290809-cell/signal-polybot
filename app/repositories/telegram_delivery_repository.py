"""Persistence for the Telegram delivery queue.

Idempotency lives in the database (unique ``idempotency_key``), so a delivery
survives retries and process restarts without ever being sent twice.  Workers
claim due deliveries with a lease; expired PROCESSING leases are reclaimed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import TelegramDeliveryStatus
from app.repositories.orm import TelegramDelivery

OPEN_STATUSES = (
    TelegramDeliveryStatus.PENDING.value,
    TelegramDeliveryStatus.RETRYING.value,
    TelegramDeliveryStatus.PROCESSING.value,
)
_CLAIMABLE = (TelegramDeliveryStatus.PENDING.value, TelegramDeliveryStatus.RETRYING.value)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class TelegramDeliveryRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # -------------------------------------------------------------- enqueue

    async def insert_if_new(self, fields: dict[str, Any]) -> tuple[TelegramDelivery, bool]:
        """Insert a delivery unless its idempotency_key already exists.

        Returns (row, created).  Uniqueness is enforced by the DB constraint,
        so concurrent inserts of the same key cannot both succeed.
        """
        async with self._session_factory() as session:
            existing = await self._by_key(session, fields["idempotency_key"])
            if existing is not None:
                return existing, False
            now = _now()
            row = TelegramDelivery(
                delivery_id=uuid.uuid4(), created_at=now, updated_at=now, **fields
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self._by_key(session, fields["idempotency_key"])
                assert existing is not None
                return existing, False
            return row, True

    async def _by_key(self, session: AsyncSession, key: str) -> TelegramDelivery | None:
        return (
            await session.execute(
                sa.select(TelegramDelivery).where(TelegramDelivery.idempotency_key == key)
            )
        ).scalar_one_or_none()

    async def get_by_key(self, key: str) -> TelegramDelivery | None:
        async with self._session_factory() as session:
            return await self._by_key(session, key)

    async def get(self, delivery_id: uuid.UUID) -> TelegramDelivery | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(TelegramDelivery).where(TelegramDelivery.delivery_id == delivery_id)
                )
            ).scalar_one_or_none()

    # ---------------------------------------------------------------- claim

    async def claim_due(
        self, limit: int, lease_seconds: float, now: datetime | None = None
    ) -> list[TelegramDelivery]:
        """Reclaim expired leases, then claim due deliveries (priority first)."""
        now = now or _now()
        async with self._session_factory() as session:
            # lease recovery: PROCESSING with expired lease goes back to RETRYING
            await session.execute(
                sa.update(TelegramDelivery)
                .where(
                    TelegramDelivery.status == TelegramDeliveryStatus.PROCESSING.value,
                    TelegramDelivery.lease_expires_at.is_not(None),
                    TelegramDelivery.lease_expires_at < now,
                )
                .values(status=TelegramDeliveryStatus.RETRYING.value, lease_expires_at=None)
            )
            rows = (
                (
                    await session.execute(
                        sa.select(TelegramDelivery)
                        .where(
                            TelegramDelivery.status.in_(_CLAIMABLE),
                            sa.or_(
                                TelegramDelivery.scheduled_at.is_(None),
                                TelegramDelivery.scheduled_at <= now,
                            ),
                        )
                        .order_by(
                            TelegramDelivery.priority,
                            TelegramDelivery.created_at,
                            TelegramDelivery.id,
                        )
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = TelegramDeliveryStatus.PROCESSING.value
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.attempt_count += 1
            await session.commit()
            return list(rows)

    # ------------------------------------------------------------- outcomes

    async def _update(self, delivery_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self._session_factory() as session:
            await session.execute(
                sa.update(TelegramDelivery)
                .where(TelegramDelivery.delivery_id == delivery_id)
                .values(**values)
            )
            await session.commit()

    async def mark_sent(self, delivery_id: uuid.UUID, message_id: int) -> None:
        await self._update(
            delivery_id,
            {
                "status": TelegramDeliveryStatus.SENT.value,
                "message_id": message_id,
                "sent_at": _now(),
                "lease_expires_at": None,
            },
        )

    async def mark_edited(self, delivery_id: uuid.UUID) -> None:
        await self._update(
            delivery_id,
            {
                "status": TelegramDeliveryStatus.EDITED.value,
                "sent_at": _now(),
                "lease_expires_at": None,
            },
        )

    async def mark_retrying(
        self, delivery_id: uuid.UUID, next_attempt_at: datetime, error_class: str
    ) -> None:
        await self._update(
            delivery_id,
            {
                "status": TelegramDeliveryStatus.RETRYING.value,
                "scheduled_at": next_attempt_at,
                "lease_expires_at": None,
                "last_error_class": error_class,
                "last_error_at": _now(),
            },
        )

    async def mark_failed(self, delivery_id: uuid.UUID, error_class: str, note: str = "") -> None:
        await self._update(
            delivery_id,
            {
                "status": TelegramDeliveryStatus.FAILED.value,
                "lease_expires_at": None,
                "last_error_class": error_class,
                "last_error_at": _now(),
                "error": note[:500],
            },
        )

    async def mark_dead_letter(self, delivery_id: uuid.UUID, error_class: str) -> None:
        await self._update(
            delivery_id,
            {
                "status": TelegramDeliveryStatus.DEAD_LETTER.value,
                "lease_expires_at": None,
                "last_error_class": error_class,
                "last_error_at": _now(),
            },
        )

    async def cancel_lowest_priority(self, below_priority: int, count: int = 1) -> int:
        """Cancel up to ``count`` open deliveries with priority > ``below_priority``.

        Used on queue overflow: low-priority WATCHLIST/DAILY messages are
        dropped first; priorities 1 and 2 are never dropped here.
        """
        cancelled = 0
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(TelegramDelivery)
                        .where(
                            TelegramDelivery.status.in_(_CLAIMABLE),
                            TelegramDelivery.priority > max(below_priority, 2),
                        )
                        .order_by(
                            TelegramDelivery.priority.desc(),
                            TelegramDelivery.created_at,
                        )
                        .limit(count)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = TelegramDeliveryStatus.CANCELLED.value
                row.last_error_class = "queue_overflow"
                row.last_error_at = _now()
                cancelled += 1
            await session.commit()
        return cancelled

    # ---------------------------------------------------------------- reads

    async def open_count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count())
                .select_from(TelegramDelivery)
                .where(TelegramDelivery.status.in_(OPEN_STATUSES))
            )
            return int(value.scalar_one())

    async def counts_by_status(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(TelegramDelivery.status, sa.func.count()).group_by(
                    TelegramDelivery.status
                )
            )
            return {status: int(count) for status, count in rows.all()}

    async def dead_letter_count(self) -> int:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.count())
                .select_from(TelegramDelivery)
                .where(TelegramDelivery.status == TelegramDeliveryStatus.DEAD_LETTER.value)
            )
            return int(value.scalar_one())

    async def last_success_at(self) -> datetime | None:
        async with self._session_factory() as session:
            value = await session.execute(
                sa.select(sa.func.max(TelegramDelivery.sent_at)).where(
                    TelegramDelivery.status.in_(
                        [
                            TelegramDeliveryStatus.SENT.value,
                            TelegramDeliveryStatus.EDITED.value,
                        ]
                    )
                )
            )
            result: datetime | None = value.scalar_one_or_none()
            return result

    async def find_recent_duplicate(
        self, content_hash: str, since: datetime
    ) -> TelegramDelivery | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    sa.select(TelegramDelivery)
                    .where(
                        TelegramDelivery.content_hash == content_hash,
                        TelegramDelivery.created_at >= since,
                        TelegramDelivery.status.notin_(
                            [
                                TelegramDeliveryStatus.FAILED.value,
                                TelegramDeliveryStatus.DEAD_LETTER.value,
                                TelegramDeliveryStatus.CANCELLED.value,
                                TelegramDeliveryStatus.SKIPPED_DUPLICATE.value,
                            ]
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def recent(self, limit: int = 50) -> list[TelegramDelivery]:
        async with self._session_factory() as session:
            rows = await session.execute(
                sa.select(TelegramDelivery)
                .order_by(TelegramDelivery.created_at.desc(), TelegramDelivery.id.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())
