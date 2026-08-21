"""Status endpoint: bot state, universe, session, open signals, data times."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from app.domain.enums import BotState

router = APIRouter()


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    settings = ctx.settings

    bot_state = BotState.PAUSED if settings.app.kill_switch else BotState.RUNNING

    instruments = await ctx.instrument_repo.list_all()
    universe = [
        {
            "symbol": row.symbol,
            "instrument_id": row.instrument_id,
            "asset_class": row.asset_class,
            "status": row.status,
            "enabled": row.enabled,
            "max_leverage": row.max_leverage,
            "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        }
        for row in instruments
    ]
    open_signals = await ctx.signal_repo.count_open()
    last_refresh = ctx.instrument_service.last_refresh_at

    return {
        "bot_state": bot_state.value,
        "environment": settings.app.environment,
        "config_version": settings.app.config_version,
        "strategy_version": settings.app.strategy_version,
        "started_at": ctx.started_at.isoformat(),
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "session": None,  # session-aware scheduler arrives in phase 7
        "universe": universe,
        "open_signals": open_signals,
        "last_universe_refresh": last_refresh.isoformat() if last_refresh else None,
    }
