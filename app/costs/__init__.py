"""Cost engine (phase 9): fees, slippage, funding, net expectancy.

Pure domain/application logic over immutable public-market-data snapshots.
No network calls, no Telegram, no order placement, no account data.  All
outputs are internal research estimates - never trade instructions and never
a promise about realised costs or returns.
"""
