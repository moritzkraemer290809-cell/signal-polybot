"""Deterministic walk-forward evaluation (pure, no I/O).

Splits the window into ascending train/validation/test segments.  A
configuration is chosen ONLY from train+validation evidence; the test
window is never used for selection and is evaluated once, afterwards.
V1 takes an explicit, finite list of candidate configurations - there is
no free parameter search and no optimisation on test data.

Selection never ranks by result alone: data completeness, sample size,
drawdown, net R, cost share and dispersion all enter a documented,
deterministic score, and too few complete simulations yield
INSUFFICIENT_SAMPLE instead of a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import BacktestSettings
from app.simulation.enums import MetricSampleStatus, WalkForwardMode, WalkForwardWindow


@dataclass(frozen=True)
class WalkForwardSegment:
    """One ascending window of a split."""

    window: WalkForwardWindow
    start_at: datetime
    end_at: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start_at <= moment < self.end_at


@dataclass(frozen=True)
class WalkForwardSplit:
    """Train/validation/test triple with a fixed, ascending order."""

    index: int
    mode: WalkForwardMode
    train: WalkForwardSegment
    validation: WalkForwardSegment
    test: WalkForwardSegment

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "mode": self.mode.value,
            "train": [self.train.start_at.isoformat(), self.train.end_at.isoformat()],
            "validation": [
                self.validation.start_at.isoformat(),
                self.validation.end_at.isoformat(),
            ],
            "test": [self.test.start_at.isoformat(), self.test.end_at.isoformat()],
        }

    def leaks_test_data(self) -> bool:
        """True when selection evidence could overlap the test window."""
        return (
            self.train.end_at > self.test.start_at
            or self.validation.end_at > self.test.start_at
            or self.train.start_at >= self.test.start_at
        )


@dataclass(frozen=True)
class ConfigurationCandidate:
    """One explicitly listed configuration to evaluate (no free search)."""

    key: str
    parameters: dict[str, Any]
    description: str = ""


@dataclass(frozen=True)
class SegmentEvidence:
    """Hypothetical evidence of ONE configuration on ONE window."""

    configuration_key: str
    window: WalkForwardWindow
    complete_simulations: int
    incomplete_simulations: int
    rejected_simulations: int
    average_net_r: float | None
    median_net_r: float | None
    max_drawdown_r: float | None
    cost_share_pct: float | None
    data_completeness_rate: float | None

    @property
    def total(self) -> int:
        return self.complete_simulations + self.incomplete_simulations + self.rejected_simulations


@dataclass(frozen=True)
class SelectionResult:
    """Deterministic configuration choice with its full rationale."""

    selected_key: str | None
    sample_status: MetricSampleStatus
    rationale: str
    scores: tuple[tuple[str, float], ...]
    considered: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    criteria: dict[str, Any] = field(default_factory=dict)


def build_splits(
    *,
    start_at: datetime,
    end_at: datetime,
    settings: BacktestSettings,
) -> tuple[WalkForwardSplit, ...]:
    """Ascending rolling or expanding splits inside the window."""
    train = timedelta(days=settings.walk_forward_train_days)
    validation = timedelta(days=settings.walk_forward_validation_days)
    test = timedelta(days=settings.walk_forward_test_days)
    step = timedelta(days=settings.walk_forward_step_days)
    mode = WalkForwardMode(settings.walk_forward_mode)

    splits: list[WalkForwardSplit] = []
    index = 0
    cursor = start_at
    while cursor + train + validation + test <= end_at:
        train_start = start_at if mode is WalkForwardMode.EXPANDING else cursor
        train_end = cursor + train
        validation_end = train_end + validation
        test_end = validation_end + test
        splits.append(
            WalkForwardSplit(
                index=index,
                mode=mode,
                train=WalkForwardSegment(WalkForwardWindow.TRAIN, train_start, train_end),
                validation=WalkForwardSegment(
                    WalkForwardWindow.VALIDATION, train_end, validation_end
                ),
                test=WalkForwardSegment(WalkForwardWindow.TEST, validation_end, test_end),
            )
        )
        index += 1
        cursor += step
    return tuple(splits)


def _normalised(value: float | None, *, best: float, worst: float) -> float:
    """Map a metric onto [0, 1] where 1 is the better end (deterministic)."""
    if value is None or best == worst:
        return 0.0
    clamped = max(min(value, max(best, worst)), min(best, worst))
    return (clamped - worst) / (best - worst)


def select_configuration(
    evidence: tuple[SegmentEvidence, ...],
    *,
    min_simulations: int,
    candidates: tuple[ConfigurationCandidate, ...],
) -> SelectionResult:
    """Pick one configuration from TRAIN+VALIDATION evidence only.

    Never "highest return wins": the score blends sample size, data
    completeness, net R, drawdown and cost share, and any configuration
    without enough complete simulations is excluded.
    """
    considered = tuple(candidate.key for candidate in candidates)
    selection_evidence = [
        item
        for item in evidence
        if item.window in (WalkForwardWindow.TRAIN, WalkForwardWindow.VALIDATION)
    ]
    if any(item.window is WalkForwardWindow.TEST for item in evidence):
        # defensive: test evidence must never influence the choice
        selection_evidence = [
            item for item in selection_evidence if item.window is not WalkForwardWindow.TEST
        ]

    per_config: dict[str, list[SegmentEvidence]] = {}
    for item in selection_evidence:
        per_config.setdefault(item.configuration_key, []).append(item)

    scores: list[tuple[str, float]] = []
    warnings: list[str] = []
    for key in considered:
        items = per_config.get(key, [])
        complete = sum(item.complete_simulations for item in items)
        if complete < min_simulations:
            warnings.append(f"{key}: only {complete} complete simulations")
            continue
        net_values = [item.average_net_r for item in items if item.average_net_r is not None]
        drawdowns = [item.max_drawdown_r for item in items if item.max_drawdown_r is not None]
        costs = [item.cost_share_pct for item in items if item.cost_share_pct is not None]
        completeness = [
            item.data_completeness_rate for item in items if item.data_completeness_rate is not None
        ]
        average_net = sum(net_values) / len(net_values) if net_values else None
        worst_drawdown = min(drawdowns) if drawdowns else None
        cost_share = sum(costs) / len(costs) if costs else None
        completeness_rate = sum(completeness) / len(completeness) if completeness else None
        score = (
            0.35 * _normalised(average_net, best=1.0, worst=-1.0)
            + 0.25 * _normalised(worst_drawdown, best=0.0, worst=-5.0)
            + 0.20 * _normalised(completeness_rate, best=1.0, worst=0.0)
            + 0.10 * _normalised(cost_share, best=0.0, worst=50.0)
            + 0.10 * _normalised(float(complete), best=float(4 * min_simulations), worst=0.0)
        )
        scores.append((key, round(score, 6)))

    if not scores:
        return SelectionResult(
            selected_key=None,
            sample_status=MetricSampleStatus.INSUFFICIENT_SAMPLE,
            rationale=(
                "no configuration reached the minimum number of complete hypothetical "
                f"simulations ({min_simulations}) on train/validation evidence"
            ),
            scores=(),
            considered=considered,
            warnings=tuple(warnings),
            criteria={"min_simulations": min_simulations},
        )

    # deterministic ordering: score desc, then key asc
    scores.sort(key=lambda item: (-item[1], item[0]))
    selected = scores[0][0]
    return SelectionResult(
        selected_key=selected,
        sample_status=MetricSampleStatus.SUFFICIENT,
        rationale=(
            f"selected {selected} from train/validation evidence using the documented "
            "multi-criteria score (net R, drawdown, data completeness, cost share, "
            "sample size) - never by result alone, and never using test-window data"
        ),
        scores=tuple(scores),
        considered=considered,
        warnings=tuple(warnings),
        criteria={
            "weights": {
                "average_net_r": 0.35,
                "max_drawdown_r": 0.25,
                "data_completeness": 0.20,
                "cost_share": 0.10,
                "sample_size": 0.10,
            },
            "min_simulations": min_simulations,
            "tie_breaker": "configuration key ascending",
        },
    )
