"""Hypothetical performance metrics over modelled simulations.

Every metric describes MODELLED outcomes of a hypothetical, delayed
follower simulation - never real performance, never a forecast and never
a guarantee.  Metric names carry "hypothetical"/"simulated"/"modelled"
wherever a user can see them.  Below the configured minimum sample size a
metric set is flagged INSUFFICIENT_SAMPLE and ratio metrics are withheld.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median

from app.simulation.bootstrap_statistics import bootstrap_mean
from app.simulation.drawdown import (
    R_BASIS,
    VIRTUAL_BASIS,
    compute_drawdown,
    equity_curve,
    max_consecutive_losses,
)
from app.simulation.enums import DataCompleteness, MetricSampleStatus, SimulatedPositionState
from app.simulation.models import (
    BootstrapResult,
    DrawdownResult,
    MetricSet,
    MetricValue,
    SimulatedPositionResult,
)
from app.simulation.version import METRIC_DEFINITION_VERSION

#: metrics suppressed when the sample is too small to interpret
RATIO_METRICS = frozenset(
    {
        "hypothetical_win_rate",
        "profit_factor_hypothetical",
        "expectancy_net_r",
        "costs_as_pct_of_gross_result",
    }
)


def _completed(results: Sequence[SimulatedPositionResult]) -> list[SimulatedPositionResult]:
    return [item for item in results if item.state is SimulatedPositionState.CLOSED_SIMULATION]


def _value(name: str, value: float | None, unit: str, detail: str = "") -> MetricValue:
    return MetricValue(name=name, value=value, unit=unit, detail=detail)


def compute_metrics(
    results: Sequence[SimulatedPositionResult],
    *,
    rejected: int = 0,
    min_complete_simulations: int = 20,
    virtual_account_pusd: float | None = None,
    metrics_version: str = METRIC_DEFINITION_VERSION,
    disclaimer_version: str = "sdv-1",
    segment_kind: str = "ALL",
    segment_key: str = "ALL",
    bootstrap_resamples: int = 0,
    bootstrap_seed: int = 0,
) -> MetricSet:
    """All base metrics of one segment of hypothetical simulations."""
    total = len(results) + rejected
    completed = _completed(results)
    incomplete = [
        item
        for item in results
        if item.state
        in (
            SimulatedPositionState.INCOMPLETE,
            SimulatedPositionState.UNMODELED_EXIT,
            SimulatedPositionState.OPEN_SIMULATION,
        )
    ]
    net_values = [item.net_result for item in completed if item.net_result is not None]
    net_r_values = [item.net_r for item in completed if item.net_r is not None]
    gross_values = [item.gross_result for item in completed if item.gross_result is not None]
    gross_r_values = [item.gross_r for item in completed if item.gross_r is not None]
    durations = [item.duration_seconds for item in completed if item.duration_seconds is not None]

    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    break_even = [value for value in net_values if value == 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    fees = sum(item.costs.fees_total for item in completed if item.costs is not None)
    slippage = sum(item.costs.slippage_total for item in completed if item.costs is not None)
    funding = sum(item.costs.funding_cost for item in completed if item.costs is not None)
    cost_total = fees + slippage + funding
    risk_total = sum(
        (item.entry.quantity * _risk_per_unit(item)) for item in completed if item.entry is not None
    )

    sample_status = (
        MetricSampleStatus.SUFFICIENT
        if len(completed) >= min_complete_simulations
        else MetricSampleStatus.INSUFFICIENT_SAMPLE
    )

    r_curve = equity_curve(net_r_values)
    drawdown_r = compute_drawdown(r_curve, basis=R_BASIS)
    drawdown_virtual: DrawdownResult | None = None
    if virtual_account_pusd and virtual_account_pusd > 0 and net_values:
        drawdown_virtual = compute_drawdown(
            equity_curve(net_values, starting_value=virtual_account_pusd),
            basis=VIRTUAL_BASIS,
            percentage_reference=virtual_account_pusd,
        )

    complete_data = [
        item for item in completed if item.data_completeness is DataCompleteness.COMPLETE
    ]

    values: list[MetricValue] = [
        _value("simulations_total", float(total), "count"),
        _value("simulations_completed", float(len(completed)), "count"),
        _value("simulations_incomplete", float(len(incomplete)), "count"),
        _value("simulations_rejected", float(rejected), "count"),
        _value("simulated_win_count", float(len(wins)), "count"),
        _value("simulated_loss_count", float(len(losses)), "count"),
        _value("simulated_break_even_count", float(len(break_even)), "count"),
        _value(
            "hypothetical_win_rate",
            (len(wins) / len(completed) * 100.0) if completed else None,
            "percent",
            "share of modelled simulations with a positive modelled net result",
        ),
        _value("average_gross_r", _mean(gross_r_values), "R"),
        _value("average_net_r", _mean(net_r_values), "R"),
        _value("median_net_r", _median(net_r_values), "R"),
        _value(
            "expectancy_net_r",
            _mean(net_r_values),
            "R",
            "modelled average net result per hypothetical simulation",
        ),
        _value("gross_result", _sum_or_none(gross_values), "virtual_units"),
        _value("net_result", _sum_or_none(net_values), "virtual_units"),
        _value("gross_profit", gross_profit if wins else None, "virtual_units"),
        _value("gross_loss", gross_loss if losses else None, "virtual_units"),
        _value(
            "profit_factor_hypothetical",
            (gross_profit / gross_loss) if gross_loss > 0 else None,
            "ratio",
            "modelled gross gains over modelled gross losses - hypothetical only",
        ),
        _value(
            "max_drawdown_r",
            drawdown_r.max_drawdown if drawdown_r else None,
            "R",
            "deepest modelled decline of the hypothetical R curve",
        ),
        _value(
            "max_drawdown_pct_on_virtual_account",
            drawdown_virtual.max_drawdown_pct if drawdown_virtual else None,
            "percent",
            "against the VIRTUAL reference account - never a real account",
        ),
        _value(
            "maximum_consecutive_losses",
            float(max_consecutive_losses(net_values)) if net_values else None,
            "count",
        ),
        _value("average_duration", _mean(durations), "seconds"),
        _value("median_duration", _median(durations), "seconds"),
        _value("cost_total", cost_total if completed else None, "virtual_units"),
        _value("fees_total", fees if completed else None, "virtual_units"),
        _value("slippage_total", slippage if completed else None, "virtual_units"),
        _value("funding_total", funding if completed else None, "virtual_units"),
        _value(
            "costs_as_pct_of_gross_result",
            (cost_total / abs(sum(gross_values)) * 100.0)
            if gross_values and sum(gross_values) != 0
            else None,
            "percent",
        ),
        _value(
            "costs_as_pct_of_risk",
            (cost_total / risk_total * 100.0) if risk_total > 0 else None,
            "percent",
        ),
        _value(
            "incomplete_data_rate",
            ((len(completed) - len(complete_data)) / len(completed) * 100.0) if completed else None,
            "percent",
        ),
        _value("rejection_rate", (rejected / total * 100.0) if total else None, "percent"),
    ]

    warnings: list[str] = []
    if sample_status is MetricSampleStatus.INSUFFICIENT_SAMPLE:
        warnings.append(
            f"INSUFFICIENT_SAMPLE: {len(completed)} complete hypothetical simulations "
            f"(minimum {min_complete_simulations}) - ratio metrics are withheld and "
            "no conclusion about setup quality may be drawn"
        )
        values = [
            _value(item.name, None, item.unit, "withheld: insufficient sample")
            if item.name in RATIO_METRICS
            else item
            for item in values
        ]

    bootstrap = None
    if bootstrap_resamples > 0 and sample_status is MetricSampleStatus.SUFFICIENT:
        bootstrap = bootstrap_mean(
            net_r_values,
            resamples=bootstrap_resamples,
            seed=bootstrap_seed,
            min_sample=min_complete_simulations,
        )
    if bootstrap is not None:
        values.extend(
            [
                _value("bootstrap_net_r_lower", bootstrap.lower_bound, "R", bootstrap.note),
                _value("bootstrap_net_r_upper", bootstrap.upper_bound, "R", bootstrap.note),
            ]
        )

    return MetricSet(
        segment_kind=segment_kind,
        segment_key=segment_key,
        sample_status=sample_status.value,
        complete_simulations=len(completed),
        values=tuple(values),
        metrics_version=metrics_version,
        disclaimer_version=disclaimer_version,
        warnings=tuple(warnings),
    )


def bootstrap_for(
    results: Sequence[SimulatedPositionResult],
    *,
    resamples: int,
    seed: int,
    min_sample: int,
) -> BootstrapResult | None:
    values = [item.net_r for item in _completed(results) if item.net_r is not None]
    return bootstrap_mean(values, resamples=resamples, seed=seed, min_sample=min_sample)


def _risk_per_unit(item: SimulatedPositionResult) -> float:
    metadata = item.metadata or {}
    risk = metadata.get("risk_per_unit")
    if isinstance(risk, int | float) and risk > 0:
        return float(risk)
    if item.net_result is not None and item.net_r not in (None, 0) and item.entry is not None:
        return abs(item.net_result / item.net_r / item.entry.quantity)
    return 0.0


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    return float(median(values)) if values else None


def _sum_or_none(values: Sequence[float]) -> float | None:
    return sum(values) if values else None
