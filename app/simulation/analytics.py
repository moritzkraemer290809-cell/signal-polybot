"""Segmented analytics over hypothetical simulations.

Every segment is evaluated with the SAME metric definitions and the same
sample-size rule, so a thin segment can never look stronger than it is.
Segment keys are deterministic strings; nothing here interprets results
as real performance.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from app.simulation.metrics import compute_metrics
from app.simulation.models import MetricSet, SimulatedPositionResult

#: segment name -> deterministic key extractor
SEGMENT_DIMENSIONS: dict[str, Callable[[SimulatedPositionResult], str]] = {
    "SYMBOL": lambda item: item.symbol,
    "ASSET_CLASS": lambda item: item.asset_class,
    "SESSION_STATE": lambda item: str(item.metadata.get("session_state", "UNKNOWN")),
    "MARKET_REGIME": lambda item: str(item.metadata.get("market_regime", "UNKNOWN")),
    "CANDIDATE_TYPE": lambda item: item.candidate_type,
    "RESEARCH_DIRECTION": lambda item: item.direction,
    "SETUP_SCORE_BUCKET": lambda item: _bucket(item.metadata.get("setup_score"), (75, 85, 95)),
    "ELIGIBILITY_SCORE_BUCKET": lambda item: _bucket(
        item.metadata.get("eligibility_score"), (75, 85, 95)
    ),
    "STRATEGY_VERSION": lambda item: str(item.metadata.get("strategy_version", "unknown")),
    "RISK_MODEL_VERSION": lambda item: str(item.metadata.get("risk_model_version", "unknown")),
    "COST_MODEL_VERSION": lambda item: str(item.metadata.get("cost_model_version", "unknown")),
    "FEE_SCHEDULE_VERSION": lambda item: str(item.metadata.get("fee_schedule_version", "unknown")),
    "DELAY_MODEL": lambda item: item.delay.model.value if item.delay else "UNKNOWN",
    "EXIT_REASON": lambda item: item.exit_reason.value if item.exit_reason else "NONE",
    "DATA_COMPLETENESS": lambda item: item.data_completeness.value,
    "PERIOD_DAY": lambda item: item.event_reference_at.date().isoformat(),
}


def _bucket(value: object, edges: tuple[int, ...]) -> str:
    if not isinstance(value, int | float):
        return "unknown"
    for edge in edges:
        if value < edge:
            return f"lt_{edge}"
    return f"ge_{edges[-1]}"


def segment_metrics(
    results: Sequence[SimulatedPositionResult],
    *,
    dimensions: Sequence[str] | None = None,
    min_complete_simulations: int = 20,
    virtual_account_pusd: float | None = None,
    metrics_version: str,
    disclaimer_version: str,
) -> tuple[MetricSet, ...]:
    """Metric sets per requested segmentation dimension."""
    wanted = list(dimensions or SEGMENT_DIMENSIONS.keys())
    sets: list[MetricSet] = []
    for dimension in wanted:
        extractor = SEGMENT_DIMENSIONS.get(dimension)
        if extractor is None:
            continue
        grouped: dict[str, list[SimulatedPositionResult]] = {}
        for item in results:
            grouped.setdefault(extractor(item), []).append(item)
        for key in sorted(grouped):
            sets.append(
                compute_metrics(
                    grouped[key],
                    min_complete_simulations=min_complete_simulations,
                    virtual_account_pusd=virtual_account_pusd,
                    metrics_version=metrics_version,
                    disclaimer_version=disclaimer_version,
                    segment_kind=dimension,
                    segment_key=key,
                )
            )
    return tuple(sets)
