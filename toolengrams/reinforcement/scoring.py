"""Scoring primitives: usefulness, recency, final_score.

Pure functions — no DB, no side effects. Used by retrieval/rank.py when
ranking candidates and by consolidation when reporting memory health.
"""

from __future__ import annotations

import math

from ..models import Candidate, MemoryType

# How quickly unused memories decay.
# feedback decays faster (30d) because stale corrections are harmful;
# reference memories are more evergreen (60d).
HALF_LIFE_DAYS: dict[MemoryType, float] = {
    "feedback": 30.0,
    "reference": 60.0,
}


def usefulness(useful_count: int, surface_count: int) -> float:
    """Laplace-smoothed usefulness ratio. Always in (0, 1]."""
    return (useful_count + 1.0) / (surface_count + 2.0)


def recency(last_surfaced_ts: int, half_life_days: float, now_ts: int) -> float:
    """Exponential decay on last surface time. Returns 1.0 if never surfaced (fresh)."""
    if last_surfaced_ts == 0:
        return 1.0
    if half_life_days <= 0:
        return 1.0
    delta_days = max(0.0, (now_ts - last_surfaced_ts) / 86400.0)
    return math.exp(-delta_days / half_life_days)


def final_score(candidate: Candidate, now_ts: int) -> float:
    """Combine structural match with reinforcement-weighted modifiers."""
    u = usefulness(candidate.useful_count, candidate.surface_count)
    half_life = HALF_LIFE_DAYS.get(candidate.type, HALF_LIFE_DAYS["reference"])
    r = recency(candidate.last_surfaced_ts, half_life, now_ts)
    score = candidate.structural_match * (0.5 + u) * (0.5 + 0.5 * r)
    if candidate.pinned:
        score *= 1.5
    return score
