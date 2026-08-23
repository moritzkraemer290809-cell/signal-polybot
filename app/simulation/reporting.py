"""Local report assembly and export preparation.

Reports are built locally and returned as plain structures.  Nothing is
sent anywhere: export files are written only on an explicit local admin
action and only inside the repository, and both mandatory disclaimers
travel with every payload.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from typing import Any

from app.simulation.explainability import (
    BACKTEST_DISCLAIMER,
    SIMULATION_DISCLAIMER,
    metric_set_view,
    simulation_row,
)
from app.simulation.models import MetricSet, SimulatedPositionResult


def build_report(
    *,
    run_payload: dict[str, Any],
    manifest: dict[str, Any],
    simulations: Sequence[SimulatedPositionResult],
    metric_sets: Sequence[MetricSet],
    excluded_intervals: Sequence[Any] = (),
    data_quality_warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """One complete local research report (hypothetical results only)."""
    return {
        "disclaimers": [SIMULATION_DISCLAIMER, BACKTEST_DISCLAIMER],
        "run": run_payload,
        "manifest": manifest,
        "simulations": [simulation_row(item) for item in simulations],
        "metrics": [metric_set_view(item) for item in metric_sets],
        "excluded_intervals": [
            {
                "symbol": interval.symbol,
                "channel": interval.channel,
                "start_at": interval.start_at.isoformat(),
                "end_at": interval.end_at.isoformat(),
                "detail": interval.detail,
            }
            for interval in excluded_intervals
        ],
        "data_quality_warnings": list(data_quality_warnings),
        "note": (
            "Alle Werte sind modellierte Simulationsergebnisse unter dokumentierten "
            "Annahmen - keine reale Ausfuehrung und keine Aussage ueber reale Resultate."
        ),
    }


def report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, default=str)


def simulations_to_csv(simulations: Sequence[SimulatedPositionResult]) -> str:
    """CSV text of hypothetical simulations (local preparation only)."""
    rows = [simulation_row(item) for item in simulations]
    buffer = io.StringIO()
    buffer.write(f"# {SIMULATION_DISCLAIMER}\n")
    buffer.write(f"# {BACKTEST_DISCLAIMER}\n")
    if not rows:
        return buffer.getvalue()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _flatten(value) for key, value in row.items()})
    return buffer.getvalue()


def _flatten(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return "; ".join(str(item) for item in value)
    return value
