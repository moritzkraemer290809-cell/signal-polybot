"""Market selection refresh job.

Thin structural alias: the actual scheduler/coordinator lives in
:mod:`app.selection.selection_scheduler` (lock, interval, immediate trigger).
"""

from __future__ import annotations

from app.selection.selection_scheduler import MarketSelectionCoordinator

MarketSelectionRefreshJob = MarketSelectionCoordinator

__all__ = ["MarketSelectionCoordinator", "MarketSelectionRefreshJob"]
