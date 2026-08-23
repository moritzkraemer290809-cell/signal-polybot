"""Human-readable lifecycle renderings.

Every rendering is research wording: technical development of an internal
signal, never a trade, never a position, never a promise.  Reference price
levels appear ONLY in the local dashboard payload, clearly labelled as
model values - /status carries no prices at all.
"""

from __future__ import annotations

from typing import Any

from app.signals.state_machine import transition_map_document

DISCLAIMER = "Internal Research Lifecycle - kein Handelssignal, keine reale Position."


def status_row(row: Any) -> dict[str, Any]:
    """Status-level view WITHOUT any price levels."""
    return {
        "signal": str(row.id)[:8],
        "state": row.state,
        "candidate_type": f"{row.candidate_type} ({row.direction} Research Context)",
        "symbol": row.symbol,
        "last_evaluated_at": (row.last_evaluated_at.isoformat() if row.last_evaluated_at else None),
        "data_quality": row.last_data_quality_status,
        "session": row.last_session_state,
        "expires_at": row.expires_at.isoformat(),
        "last_event": row.last_event_type,
        "note": "Research Lifecycle - kein Handelssignal.",
    }


def dashboard_row(row: Any, events: list[Any], updates: list[Any]) -> dict[str, Any]:
    """Local dashboard detail - model values clearly labelled."""
    return {
        **status_row(row),
        "state_version": row.state_version,
        "model_reference_levels": {
            "label": "Interne Modell-Referenzwerte - keine Handelsanweisung",
            **(row.reference_snapshot or {}),
        },
        "approximation_warnings": list(row.approximation_warnings or []),
        "versions": {
            "lifecycle": f"{row.lifecycle_model_name}@{row.lifecycle_model_version}",
            "lifecycle_config_hash": row.lifecycle_config_hash,
            "strategy_version": row.strategy_version,
            "risk_model_version": row.risk_model_version,
            "cost_model_version": row.cost_model_version,
            "fee_schedule_version": row.fee_schedule_version,
        },
        "correlation_id": row.correlation_id,
        "transitions": [
            {
                "event": event.event_type,
                "from": event.from_state,
                "to": event.to_state,
                "state_version": event.state_version,
                "priority": event.priority,
                "reason": (event.detail or {}).get("reason"),
                "as_of": event.as_of.isoformat(),
            }
            for event in events
        ],
        "updates": [
            {
                "type": update.update_type,
                "text": (update.detail or {}).get("text"),
                "count": update.count,
                "last_as_of": update.last_as_of.isoformat(),
            }
            for update in updates
        ],
        "disclaimer": DISCLAIMER,
    }


def state_machine_document() -> dict[str, Any]:
    return {
        "label": DISCLAIMER,
        "transitions": transition_map_document(),
        "terminal_states": [
            "TECHNICAL_EXIT",
            "INVALIDATED",
            "EXPIRED",
            "SUPERSEDED",
            "REJECTED",
            "DATA_INVALID",
        ],
        "note": (
            "Zustaende beschreiben die technische Entwicklung eines "
            "Research-Signals - nie eine reale Position oder Ausfuehrung."
        ),
    }
