"""Persistent key-value store for runtime application state."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orm import AppState


class AppStateRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(sa.select(AppState).where(AppState.key == key))
            ).scalar_one_or_none()
            return None if row is None else row.value

    async def set(self, key: str, value: dict[str, Any]) -> None:
        async with self._session_factory() as session:
            row = (
                await session.execute(sa.select(AppState).where(AppState.key == key))
            ).scalar_one_or_none()
            if row is None:
                session.add(AppState(key=key, value=value))
            else:
                row.value = value
            await session.commit()
