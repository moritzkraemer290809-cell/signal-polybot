"""Hypothetical simulation and historical backtesting (phase 11).

Two strictly separated, purely hypothetical evaluation modes:

- **Shadow mode** observes internal phase-10 research lifecycles on live
  public market data and models a time-delayed hypothetical follower.
- **Historical backtest** replays persisted public data causally and
  reuses the same delay, cost, funding and lifecycle assumptions.

Everything in this package is a model: there is no order placement, no
execution, no wallet, no key custody, no real position and no real
account.  Results are hypothetical model output and never a statement
about real performance or future outcomes.  The core is pure and
deterministic; only the context/replay adapters touch repositories.
"""
