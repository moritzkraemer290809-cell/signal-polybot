"""Typed delivery models and payloads.

Payloads are strictly typed, JSON-serialisable pydantic models.  Phase 6 never
generates real trading parameters - the signal/watchlist payloads exist only
as generic templates for later phases (and clearly marked test data).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    TELEGRAM_PRIORITY_BY_TYPE,
    TelegramDeliveryOperation,
    TelegramDeliveryType,
)


class SystemMessagePayload(BaseModel):
    """Generic system message: a title plus ordered label/value lines."""

    model_config = ConfigDict(frozen=True)

    title: str
    fields: dict[str, str] = Field(default_factory=dict)
    note: str | None = None


class DailyStatusPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    uptime_seconds: float
    ws_reconnects: int
    invalid_events: int
    quality_counts: dict[str, int] = Field(default_factory=dict)
    delivery_counts: dict[str, int] = Field(default_factory=dict)


class WatchlistPayload(BaseModel):
    """Generic watchlist template - not generated automatically in phase 6."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    note: str


class SignalPayload(BaseModel):
    """Generic signal-lifecycle template - all fields are pre-rendered strings.

    Phase 6 defines the shape only; deterministic values are computed by the
    strategy/risk phases later.  No field is invented here.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    direction: str
    signal_id: str
    status: str
    entry: str | None = None
    stop: str | None = None
    tp1: str | None = None
    tp2: str | None = None
    net_rr: str | None = None
    leverage: str | None = None
    expires_at: str | None = None
    reason: str | None = None


Payload = SystemMessagePayload | DailyStatusPayload | WatchlistPayload | SignalPayload

_PAYLOAD_KINDS: dict[str, type[BaseModel]] = {
    "system": SystemMessagePayload,
    "daily": DailyStatusPayload,
    "watchlist": WatchlistPayload,
    "signal": SignalPayload,
}


def payload_to_json(payload: Payload) -> dict[str, Any]:
    for kind, model_type in _PAYLOAD_KINDS.items():
        if isinstance(payload, model_type):
            return {"kind": kind, "data": payload.model_dump(mode="json")}
    raise TypeError(f"unknown payload type {type(payload).__name__}")


def payload_from_json(raw: dict[str, Any] | None) -> Payload | None:
    if raw is None:
        return None
    model_type = _PAYLOAD_KINDS.get(str(raw.get("kind")))
    if model_type is None:
        return None
    return model_type.model_validate(raw.get("data") or {})  # type: ignore[return-value]


def content_hash(delivery_type: TelegramDeliveryType, payload: Payload) -> str:
    """Stable hash over type + payload for the deduplication window."""
    body = json.dumps(
        {"type": delivery_type.value, "payload": payload_to_json(payload)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


class DeliveryRequest(BaseModel):
    """A request to send/edit one Telegram message via the persistent queue."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    delivery_type: TelegramDeliveryType
    payload: Payload
    idempotency_key: str
    operation: TelegramDeliveryOperation = TelegramDeliveryOperation.SEND
    chat_id: int | None = None  # None -> configured group
    message_id: int | None = None  # required for EDIT
    correlation_id: str | None = None
    signal_id: uuid.UUID | None = None
    system_event_id: int | None = None
    scheduled_at: datetime | None = None
    dedup: bool = True

    @property
    def priority(self) -> int:
        return TELEGRAM_PRIORITY_BY_TYPE[self.delivery_type]
