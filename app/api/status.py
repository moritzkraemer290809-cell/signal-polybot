"""Status endpoint: bot state, universe, connection, data quality.
No secrets, no raw market payloads."""

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

    bot_state_service = getattr(ctx, "bot_state", None)
    paused = settings.app.kill_switch
    if bot_state_service is not None:
        try:
            paused = await bot_state_service.is_paused()
        except Exception:
            paused = settings.app.kill_switch
    bot_state = BotState.PAUSED if paused else BotState.RUNNING

    # a database outage must degrade the response, never crash the endpoint
    database = "ok"
    universe: list[dict[str, Any]] | None
    try:
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
    except Exception:
        database = "unavailable"
        universe = None
    open_signals: int | None
    try:
        open_signals = await ctx.signal_repo.count_open()
    except Exception:
        database = "unavailable"
        open_signals = None
    last_refresh = ctx.instrument_service.last_refresh_at

    ws_client = getattr(ctx, "ws_client", None)
    market_data = getattr(ctx, "market_data", None)
    data_quality = getattr(ctx, "data_quality", None)

    websocket: dict[str, Any] | None = None
    if ws_client is not None:
        websocket = {
            "state": ws_client.state.value,
            "reconnects": ws_client.reconnect_count,
            "messages_received": ws_client.messages_received,
            "dropped_frames": ws_client.dropped_frames,
            "active_subscriptions": ws_client.active_subscription_count,
            "desired_subscriptions": ws_client.desired_subscription_count,
            "last_message_at": (
                ws_client.last_message_at.isoformat() if ws_client.last_message_at else None
            ),
            "connected_since": (
                ws_client.connected_since.isoformat() if ws_client.connected_since else None
            ),
        }

    market: dict[str, Any] | None = None
    if market_data is not None:
        market = {
            "active_instruments": [tracker.symbol for tracker in market_data.trackers()],
            "invalid_events_total": market_data.total_invalid_events(),
            "orderbook_resync_requests": market_data.total_resync_requests(),
            "buffers": market_data.buffers.stats() if market_data.buffers else None,
        }

    quality: dict[str, Any] | None = None
    if data_quality is not None:
        quality = data_quality.summary()

    selection = getattr(ctx, "selection", None)
    selection_status: dict[str, Any] | str
    watchlist: dict[str, Any] | None = None
    if selection is not None:
        selection_status = await selection.status_stats()
        watchlist = await selection.watchlist_details()
    elif settings.selection.enabled:
        selection_status = "enabled_not_initialized"
    else:
        selection_status = "disabled"

    display_tz_now = None
    try:
        from zoneinfo import ZoneInfo

        display_tz_now = datetime.now(tz=ZoneInfo(settings.app.display_timezone)).isoformat()
    except Exception:
        display_tz_now = None

    strategy = getattr(ctx, "strategy", None)
    strategy_status: dict[str, Any] | str
    if strategy is not None:
        strategy_status = await strategy.status_stats()
    elif settings.strategy.enabled:
        strategy_status = "enabled_not_initialized"
    else:
        strategy_status = "disabled"

    risk = getattr(ctx, "risk", None)
    risk_status: dict[str, Any] | str
    if risk is not None:
        risk_status = await risk.status_stats()
    elif settings.risk.engine_enabled:
        risk_status = "enabled_not_initialized"
    else:
        risk_status = "disabled"

    signals = getattr(ctx, "signals", None)
    signals_status: dict[str, Any] | str
    if signals is not None:
        signals_status = await signals.status_stats()
    elif settings.signals.lifecycle_enabled:
        signals_status = "enabled_not_initialized"
    else:
        signals_status = "disabled"

    telegram_subsystem = getattr(ctx, "telegram", None)
    telegram: dict[str, Any] | str
    if telegram_subsystem is not None:
        telegram = await telegram_subsystem.status_stats()
    elif settings.telegram.enabled:
        telegram = "enabled_not_initialized"
    else:
        telegram = "disabled"

    return {
        "bot_state": bot_state.value,
        "database": database,
        "environment": settings.app.environment,
        "config_version": settings.app.config_version,
        "strategy_version": settings.app.strategy_version,
        "started_at": ctx.started_at.isoformat(),
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "session": None,  # session-aware scheduler arrives in phase 7
        "universe": universe,
        "open_signals": open_signals,
        "last_universe_refresh": last_refresh.isoformat() if last_refresh else None,
        "websocket": websocket,
        "market_data": market,
        "data_quality": quality,
        "telegram": telegram,
        "display_time": display_tz_now,
        "market_selection": selection_status,
        "watchlist": watchlist,
        "strategy": strategy_status,
        "risk": risk_status,
        "signals": signals_status,
    }
