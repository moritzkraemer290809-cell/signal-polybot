"""Health endpoint: process, PostgreSQL, Redis, Telegram config, WebSocket, data freshness."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, Any]:
    ctx = request.app.state.ctx
    db_ok = await ctx.db.ping()
    redis_ok = await ctx.cache.ping()

    telegram_status = "configured" if ctx.settings.telegram.configured else "not_configured"
    last_refresh = ctx.instrument_service.last_refresh_at

    components = {
        "process": "ok",
        "postgres": "ok" if db_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
        "telegram": telegram_status,
        "websocket": "not_started",  # data layer arrives in phase 5
        "instrument_discovery": ("ok" if last_refresh is not None else "pending"),
    }
    healthy = db_ok and redis_ok
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "degraded",
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "components": components,
        "last_universe_refresh": last_refresh.isoformat() if last_refresh else None,
    }
