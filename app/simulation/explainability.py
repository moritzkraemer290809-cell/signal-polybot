"""User-facing wording for hypothetical simulations.

Two mandatory disclaimers accompany every API, dashboard, report or
export payload.  The vocabulary is deliberately restricted: modelled,
simulated, hypothetical, assumption, reference - never "profitable",
"guaranteed", "safe", "works", "real performance", "realised", "fill" or
"executed".
"""

from __future__ import annotations

from typing import Any

SIMULATION_DISCLAIMER = (
    "Hypothetische Simulation / Shadow-Auswertung. Keine reale Ausfuehrung, "
    "keine reale Position und keine Garantie zukuenftiger Ergebnisse."
)

BACKTEST_DISCLAIMER = (
    "Backtests verwenden historische Daten und modellierte Annahmen. Ergebnisse "
    "koennen durch Datenluecken, Gebuehrenannahmen, Slippage, Funding, "
    "Verzoegerung, Marktregimewechsel und nicht modellierte Ausfuehrungsfaktoren "
    "erheblich von realen Resultaten abweichen."
)

#: wording that must never appear in a user-visible simulation text
FORBIDDEN_TERMS = (
    "profitabel",
    "garantiert",
    "sicherer trade",
    "gewinnchance",
    "echte performance",
    "realisiert",
    "guaranteed",
    "profitable",
)


def disclaimers(include_backtest: bool = False) -> list[str]:
    return [SIMULATION_DISCLAIMER] + ([BACKTEST_DISCLAIMER] if include_backtest else [])


def simulation_row(result: Any) -> dict[str, Any]:
    """Dashboard row for ONE hypothetical simulation."""
    entry = result.entry
    exit_execution = result.exit_execution
    costs = result.costs
    return {
        "simulation": str(result.simulation_position_id)[:8],
        "lifecycle_signal": str(result.lifecycle_signal_id)[:8],
        "symbol": result.symbol,
        "research_direction": f"{result.direction} (Research Context)",
        "candidate_type": result.candidate_type,
        "state": result.state.value,
        "modelled_entry_price": round(entry.modelled_price, 6) if entry else None,
        "modelled_exit_price": (
            round(exit_execution.modelled_price, 6) if exit_execution else None
        ),
        "modelled_delay_seconds": (result.delay.delay_seconds if result.delay else None),
        "delay_model": (result.delay.model.value if result.delay else None),
        "modelled_gross_result": (
            round(result.gross_result, 6) if result.gross_result is not None else None
        ),
        "modelled_net_result": (
            round(result.net_result, 6) if result.net_result is not None else None
        ),
        "modelled_gross_r": round(result.gross_r, 4) if result.gross_r is not None else None,
        "modelled_net_r": round(result.net_r, 4) if result.net_r is not None else None,
        "modelled_fees": round(costs.fees_total, 6) if costs else None,
        "modelled_slippage": round(costs.slippage_total, 6) if costs else None,
        "modelled_funding": round(costs.funding_cost, 6) if costs else None,
        "modelled_duration_seconds": result.duration_seconds,
        "exit_reason": result.exit_reason.value if result.exit_reason else None,
        "data_completeness": result.data_completeness.value,
        "model_warnings": list(result.warnings),
        "disclaimer": SIMULATION_DISCLAIMER,
    }


def metric_set_view(metric_set: Any) -> dict[str, Any]:
    """Dashboard view of one hypothetical metric set."""
    return {
        "segment": f"{metric_set.segment_kind}:{metric_set.segment_key}",
        "sample_status": metric_set.sample_status,
        "complete_simulations": metric_set.complete_simulations,
        "metrics": {
            item.name: {"value": item.value, "unit": item.unit, "detail": item.detail}
            for item in metric_set.values
        },
        "warnings": list(metric_set.warnings),
        "metrics_version": metric_set.metrics_version,
        "disclaimer": SIMULATION_DISCLAIMER,
    }


def run_summary(run: Any, *, include_backtest: bool = True) -> dict[str, Any]:
    """Status view of a run - no result claims, only modelled facts."""
    return {
        "run": str(getattr(run, "run_id", ""))[:8],
        "run_type": getattr(run, "run_type", "UNKNOWN"),
        "status": getattr(run, "status", "UNKNOWN"),
        "events_replayed": getattr(run, "events_replayed", None),
        "simulations": len(getattr(run, "simulations", ()) or ()),
        "excluded_intervals": len(getattr(run, "excluded_intervals", ()) or ()),
        "warnings": list(getattr(run, "warnings", ()) or ()),
        "disclaimers": disclaimers(include_backtest),
    }


def contains_forbidden_language(text: str) -> tuple[str, ...]:
    """Terms that must never reach a user-visible simulation text."""
    lowered = text.lower()
    return tuple(term for term in FORBIDDEN_TERMS if term in lowered)
