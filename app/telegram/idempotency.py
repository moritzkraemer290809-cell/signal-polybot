"""Idempotency key construction for Telegram deliveries.

Keys are deterministic and unique per logical event, enforced by a database
unique constraint - a successfully sent delivery is never sent again, across
retries AND process restarts.
"""

from __future__ import annotations

from app.domain.enums import TelegramDeliveryType


def startup_key(app_start_correlation_id: str) -> str:
    """Exactly one startup message per application-start correlation id."""
    return f"startup:{app_start_correlation_id}"


def shutdown_key(app_start_correlation_id: str) -> str:
    return f"shutdown:{app_start_correlation_id}"


def bot_state_key(action: str, change_counter: int) -> str:
    """Pause/resume notices: one per actual state change."""
    return f"bot_state:{action}:{change_counter}"


def system_event_key(delivery_type: TelegramDeliveryType, discriminator: str) -> str:
    return f"system:{delivery_type.value}:{discriminator}"


def signal_key(delivery_type: TelegramDeliveryType, signal_short_id: str, revision: int = 0) -> str:
    """Signal lifecycle notices (used from phase 10 on)."""
    return f"signal:{delivery_type.value}:{signal_short_id}:{revision}"


def daily_key(date_iso: str) -> str:
    return f"daily:{date_iso}"
