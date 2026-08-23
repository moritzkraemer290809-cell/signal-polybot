"""Signal update construction - observation stream, aggregated.

Updates carry technical observations only, are always labelled research
lifecycle, and never claim realised PnL, execution or ownership.  No
Telegram output.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.signals.enums import SignalUpdateType
from app.signals.idempotency import update_idempotency_key


def build_update(
    *,
    signal_id: uuid.UUID,
    update_type: SignalUpdateType,
    detail: str,
    update_schema_version: str,
    as_of: datetime,
    dedupe_window_seconds: float,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "update_type": update_type.value,
        "idempotency_key": update_idempotency_key(
            signal_id=str(signal_id),
            update_type=update_type.value,
            as_of=as_of,
            window_seconds=dedupe_window_seconds,
        ),
        "detail": {
            "text": f"Research Lifecycle: {detail}",
            "schema": update_schema_version,
        },
        "as_of": as_of,
    }
