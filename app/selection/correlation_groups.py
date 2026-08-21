"""Configurable correlation groups (informational in phase 7).

Groups map symbols to a named correlation bucket (e.g. "US_TECH", "CRYPTO_L1").
Phase 7 only resolves and persists group membership so later phases can apply
correlated-risk limits; no risk decision is made here.
"""

from __future__ import annotations


class CorrelationGroups:
    def __init__(self, groups: dict[str, list[str]] | None = None) -> None:
        # group name -> symbols; resolved to symbol -> group
        self._by_symbol: dict[str, str] = {}
        for group, symbols in (groups or {}).items():
            for symbol in symbols:
                self._by_symbol[symbol.strip().upper()] = group

    def group_for(self, symbol: str) -> str | None:
        return self._by_symbol.get(symbol.strip().upper())
