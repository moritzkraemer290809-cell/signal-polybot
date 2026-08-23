"""Seeded bootstrap uncertainty for hypothetical net-R samples.

This is a descriptive uncertainty estimate of THIS sample under
resampling - not a forecast, not a probability of future outcomes and not
a statement about real results.  It runs only with an explicit seed and
only above the configured minimum sample size.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from statistics import mean, median

from app.simulation.models import BootstrapResult

UNCERTAINTY_NOTE = (
    "Statistische Unsicherheitsabschaetzung auf Basis dieser hypothetischen "
    "Stichprobe; keine Prognose zukuenftiger Ergebnisse."
)


def _percentile(ordered: list[float], percentile: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def bootstrap_mean(
    values: Sequence[float],
    *,
    resamples: int,
    seed: int,
    min_sample: int,
    lower_percentile: float = 2.5,
    upper_percentile: float = 97.5,
    statistic: str = "mean_net_r",
) -> BootstrapResult | None:
    """Percentile confidence interval of a hypothetical sample statistic."""
    sample = [float(value) for value in values]
    if len(sample) < max(2, min_sample) or resamples <= 0:
        return None
    rng = random.Random(seed)
    estimator = mean if statistic.startswith("mean") else median
    draws: list[float] = []
    size = len(sample)
    for _ in range(resamples):
        resample = [sample[rng.randrange(size)] for _ in range(size)]
        draws.append(float(estimator(resample)))
    draws.sort()
    return BootstrapResult(
        statistic=statistic,
        observed=float(estimator(sample)),
        resamples=resamples,
        seed=seed,
        lower_percentile=lower_percentile,
        upper_percentile=upper_percentile,
        lower_bound=_percentile(draws, lower_percentile),
        upper_bound=_percentile(draws, upper_percentile),
        sample_size=size,
        note=UNCERTAINTY_NOTE,
    )
