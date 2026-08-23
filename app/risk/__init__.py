"""Risk engine (phase 9): technical invalidation, reference framing,
margin/liquidation plausibility and signal eligibility plans.

Pure domain/application logic over immutable public-market-data snapshots.
No live trading, no orders, no key custody or transaction code paths of any
kind, and no account/balance/position data - every monetary figure is a
hypothetical research reference.  Outputs are internal research eligibility
assessments, never trade signals.
"""
