"""Internal signal lifecycle (phase 10).

Deterministic research/monitoring lifecycle over persisted phase-9
eligibility plans.  A lifecycle signal is an internal research object -
never a live trade, never a position, never a recommendation and never a
Telegram message.  The core is pure domain logic over immutable snapshots:
no network, no Telegram, no trading/order/account code paths.
"""
