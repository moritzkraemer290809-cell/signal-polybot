"""Async database engine / session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import DatabaseSettings


class Database:
    """Owns the async engine and session factory for the application."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        engine_kwargs: dict[str, object] = {"echo": settings.echo}
        if not settings.url.startswith("sqlite"):
            engine_kwargs["pool_size"] = settings.pool_size
            engine_kwargs["pool_pre_ping"] = True
        self._engine: AsyncEngine = create_async_engine(settings.url, **engine_kwargs)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, autoflush=False
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            yield session

    async def ping(self) -> bool:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self._engine.dispose()
