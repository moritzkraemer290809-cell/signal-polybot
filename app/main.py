"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.api.dashboard import router as dashboard_router
from app.api.health import router as health_router
from app.api.status import router as status_router
from app.bootstrap import build_context
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None, context: Any | None = None) -> FastAPI:
    """Build the FastAPI app.

    ``context`` allows tests to inject a fully faked AppContext; in that case
    no real resources (database, redis, http clients) are created.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if context is not None:
            app.state.ctx = context
            yield
            return
        ctx = await build_context(settings or get_settings())
        app.state.ctx = ctx
        try:
            yield
        finally:
            await ctx.aclose()

    app = FastAPI(
        title="polysignal-intelligence",
        version="0.1.0",
        description=(
            "Read-only Polymarket Perps market-intelligence API. "
            "No trading, no order placement, no wallet access."
        ),
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(status_router)
    app.include_router(dashboard_router)
    return app


app = create_app()
