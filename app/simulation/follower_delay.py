"""Versioned follower delay model - pure and deterministic.

A hypothetical follower never reacts at the lifecycle event instant: the
delay starts at the phase-10 ``ENTRY_CONFIRMED`` event timestamp and is
drawn from the configured, versioned model.  Random models use an
explicit seed so every run is reproducible; there is never an assumption
of a perfect entry at the original reference price.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import timedelta

from app.config import ShadowSimulationSettings
from app.simulation.enums import DelayModel
from app.simulation.models import DelayRealization, LifecycleObservation

#: hard ceiling so a pathological draw can never model an absurd delay
MAX_MODELLED_DELAY_SECONDS = 3600.0


def _seed_for(observation: LifecycleObservation, seed_base: int) -> int:
    """Deterministic per-lifecycle seed (same input -> same draw)."""
    raw = f"{seed_base}|{observation.signal_id}|{observation.state_version}"
    return int(hashlib.sha256(raw.encode()).hexdigest()[:8], 16)


def realize_delay(
    observation: LifecycleObservation,
    settings: ShadowSimulationSettings,
    *,
    seed_base: int = 0,
) -> DelayRealization:
    """Draw the modelled follower delay for one lifecycle observation."""
    model = DelayModel(settings.delay_model)
    reference = observation.as_of
    seed: int | None = None
    parameters: dict[str, float] = {}

    if model is DelayModel.EVENT_TIMESTAMP_ONLY:
        delay_seconds = 0.0
        detail = "no follower delay modelled (event timestamp basis)"
    elif model is DelayModel.FIXED_SECONDS:
        delay_seconds = float(settings.delay_fixed_seconds)
        parameters = {"fixed_seconds": delay_seconds}
        detail = f"fixed modelled follower delay of {delay_seconds:.1f}s"
    elif model is DelayModel.UNIFORM_RANGE_SECONDS:
        seed = _seed_for(observation, seed_base)
        rng = random.Random(seed)
        low = float(settings.delay_min_seconds)
        high = float(settings.delay_max_seconds)
        delay_seconds = rng.uniform(low, high)
        parameters = {"min_seconds": low, "max_seconds": high}
        detail = f"uniform modelled follower delay drawn from [{low:.1f}s, {high:.1f}s]"
    else:  # LOGNORMAL_DELAY
        seed = _seed_for(observation, seed_base)
        rng = random.Random(seed)
        mu = float(settings.delay_lognormal_mu)
        sigma = float(settings.delay_lognormal_sigma)
        delay_seconds = math.exp(rng.gauss(mu, sigma))
        parameters = {"mu": mu, "sigma": sigma}
        detail = f"lognormal modelled follower delay (mu={mu:.2f}, sigma={sigma:.2f})"

    delay_seconds = max(0.0, min(delay_seconds, MAX_MODELLED_DELAY_SECONDS))
    return DelayRealization(
        model=model,
        parameters=parameters,
        delay_seconds=delay_seconds,
        event_reference_at=reference,
        scheduled_entry_at=reference + timedelta(seconds=delay_seconds),
        seed=seed,
        detail=detail,
    )
