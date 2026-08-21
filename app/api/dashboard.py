"""Minimal local dashboard API (JSON summary; extended in later phases)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.observability.metrics import metrics

router = APIRouter()


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    enabled = await ctx.instrument_repo.list_enabled()
    return {
        "enabled_markets": [row.symbol for row in enabled],
        "open_signals": await ctx.signal_repo.count_open(),
        "metrics": metrics.snapshot(),
    }
