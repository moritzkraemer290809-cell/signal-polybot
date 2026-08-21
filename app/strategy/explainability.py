"""Deterministic reason summaries for candidates and rejections."""

from __future__ import annotations

from app.strategy.models import ScoreBreakdown


def summarize_candidate(
    candidate_type: str,
    direction: str,
    regime: str,
    htf_structure: str,
    key_evidence: list[str],
    score: int,
) -> str:
    evidence = "; ".join(key_evidence[:4]) if key_evidence else "no additional evidence"
    return (
        f"Research candidate {candidate_type} ({direction} structure) - "
        f"regime {regime}, 1h structure {htf_structure}; {evidence}; "
        f"setup score {score}/100. Research output only - not a trade signal."
    )


def summarize_components(components: list[ScoreBreakdown]) -> list[str]:
    return [
        f"{item.component}: {item.awarded}/{item.max_points} ({item.reason})" for item in components
    ]
