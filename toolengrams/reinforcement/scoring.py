"""Scoring primitives: q (noise-aware usefulness), the surfacing gate, final_score.

Pure functions — no DB, no side effects. Used by retrieval/rank.py when ranking
candidates, by hooks/pretool.py + hooks/post_tool_failure.py for the surfacing
gate, and by consolidation when reporting memory health.

v10 (design-v10 §4): one ratio, `q`, drives both ranking and the gate. Recency
was removed — surfacing is event-driven, so age is a backwards signal (a
rare-but-important memory is relevant exactly when its long-dormant trigger fires
again). Staleness is consolidation's job, not the ranker's.
"""

from __future__ import annotations

from ..models import Candidate

# Surfacing-gate knobs (design-v10 §4.2).
GATE_THRESHOLD = 0.5  # q below this ⟺ noise > helpful; the prior's mean, not tuned.
WARMUP_N = 3          # don't gate until this many verdicts, so one unlucky early
                      # 'noise' can't kill a young memory.


def q(useful_count: int, noise_count: int) -> float:
    """Noise-aware, Laplace-smoothed quality ratio in (0, 1).

    `(useful + 1) / (useful + noise + 2)` — a Beta(1,1) prior, mean ½, so a fresh
    memory (0/0) sits at exactly 0.5. `unused` verdicts enter neither counter, so
    a correct-but-situational memory is not punished for not being acted on.
    """
    return (useful_count + 1.0) / (useful_count + noise_count + 2.0)


def final_score(candidate: Candidate) -> float:
    """Rank weight: quality plus the pin boost. No recency, no structural term.

    Collapses to `(0.5 + q) · [1.5 if pinned]`. The hook sort breaks ties by
    trigger specificity first, then this score.
    """
    score = 0.5 + q(candidate.useful_count, candidate.noise_count)
    if candidate.pinned:
        score *= 1.5
    return score


def is_gated(candidate: Candidate) -> bool:
    """True if the surfacing gate should suppress this candidate.

    A `hint` that has proven more noise than signal (`q < 0.5`) after a warm-up
    of `WARMUP_N` verdicts. `block` and `pinned` memories are exempt — a safety
    rule that's rarely visibly-heeded must still fire. The gate is a hint-only
    quality valve, distinct from the sort+cap that only orders what does surface.
    """
    if candidate.kind == "block" or candidate.pinned:
        return False
    judged = candidate.useful_count + candidate.noise_count
    if judged < WARMUP_N:
        return False
    return q(candidate.useful_count, candidate.noise_count) < GATE_THRESHOLD
