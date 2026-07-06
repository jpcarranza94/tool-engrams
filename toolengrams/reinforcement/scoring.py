"""Scoring primitives: q (noise-aware usefulness), the surfacing gate, final_score.

Pure functions — no DB, no side effects. Used by retrieval/rank.py when ranking
candidates, by hooks/pretool.py + hooks/post_tool_failure.py for the surfacing
gate, and by consolidation when reporting memory health.

One ratio, `q`, drives both ranking and the gate. There is no recency term —
surfacing is event-driven, so age is a backwards signal (a rare-but-important
memory is relevant exactly when its long-dormant trigger fires again). Staleness
is consolidation's job, not the ranker's.
"""

from __future__ import annotations

from .. import envvars
from ..models import Candidate
from ..utils import env_float, env_int

# Surfacing-gate knobs (defaults; override via config.json / env — read at call
# time in is_gated so config.hydrate_env has populated the values).
GATE_THRESHOLD = 0.5  # q below this ⟺ noise > helpful; the prior's mean, not tuned.
WARMUP_N = 3          # don't gate until this many verdicts, so one unlucky early
                      # 'noise' can't kill a young memory.

# Block-kind gate (much stricter than the hint gate). A `block` DENIES calls, so
# a rare-but-correct safety rule must keep firing even with thin visible
# follow-through — hence a far higher warm-up and a floor well below the 0.5 hint
# threshold. Only a strongly net-negative, heavily-observed block is suppressed.
BLOCK_GATE_WARMUP = 12   # need this many verdicts before a block can gate at all.
BLOCK_GATE_FLOOR = 0.35  # and only when q has fallen below this (≈net-negative).


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

    Two gates, one valve. A `hint` is suppressed once it's proven more noise
    than signal (`q < GATE_THRESHOLD`) after a warm-up of `WARMUP_N` verdicts. A
    `block` is far harder to gate: because it DENIES calls, a rare-but-correct
    safety rule must keep firing even when it rarely shows visible follow-through
    — so a block gates only past a much higher warm-up (`BLOCK_GATE_WARMUP`) AND
    a much lower floor (`BLOCK_GATE_FLOOR`), catching only a strongly
    net-negative, heavily-observed block. Gating a block SUPPRESSES it (it is
    never auto-demoted to a hint). `pinned` memories are always exempt. The gate
    is a quality valve, distinct from the sort+cap that only orders what surfaces.
    """
    if candidate.pinned:
        return False
    # block vs hint differ only in two numbers: a block needs a far higher
    # warm-up and a far lower floor before it can gate at all.
    if candidate.kind == "block":
        warmup = env_int(envvars.BLOCK_GATE_WARMUP, BLOCK_GATE_WARMUP)
        floor = env_float(envvars.BLOCK_GATE_FLOOR, BLOCK_GATE_FLOOR)
    else:
        warmup = env_int(envvars.GATE_WARMUP_N, WARMUP_N)
        floor = env_float(envvars.GATE_THRESHOLD, GATE_THRESHOLD)
    if candidate.useful_count + candidate.noise_count < warmup:
        return False
    return q(candidate.useful_count, candidate.noise_count) < floor
