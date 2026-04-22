"""Retrieval tests: subsequence match + scoring primitives.

The v1 cluster-level Laplace gate was dropped in v2 (design-v9) — session
dedup + specificity sort live in the hook handlers now, not here.
"""

from __future__ import annotations

import math

from toolengrams.models import Candidate
from toolengrams.reinforcement.scoring import final_score, recency, usefulness
from toolengrams.retrieval.rank import is_subsequence

NOW = 1_780_000_000  # fixed "now" for deterministic recency math
DAY = 86_400


def _candidate(
    memory_id: int = 1,
    surface_count: int = 0,
    useful_count: int = 0,
    last_surfaced_ts: int = 0,
    pinned: bool = False,
    kind: str = "hint",
    matched_tokens: tuple[str, ...] = ("git",),
    matched_path: str | None = None,
) -> Candidate:
    return Candidate(
        memory_id=memory_id,
        name=f"m{memory_id}",
        body="body",
        matched_tokens=matched_tokens,
        matched_path=matched_path,
        surface_count=surface_count,
        useful_count=useful_count,
        last_surfaced_ts=last_surfaced_ts,
        pinned=pinned,
        kind=kind,
        scope="project",
        structural_match=1.0,
    )


# ---------- subsequence match ----------


def test_subseq_exact_match():
    assert is_subsequence(("git", "push"), ("git", "push"))


def test_subseq_with_gap():
    # The poster child: `ergeon order 12345 reassign` matches `[ergeon, order, reassign]`.
    assert is_subsequence(
        ("ergeon", "order", "reassign"),
        ("ergeon", "order", "12345", "reassign"),
    )


def test_subseq_wrong_order_fails():
    assert not is_subsequence(
        ("ergeon", "order", "reassign"),
        ("ergeon", "reassign", "order"),
    )


def test_subseq_missing_token_fails():
    assert not is_subsequence(
        ("ergeon", "order", "reassign"),
        ("ergeon", "customer", "reassign"),
    )


def test_subseq_empty_needle_fails():
    # A trigger with no tokens is meaningless; treat as no-match.
    assert not is_subsequence((), ("git", "push"))


# ---------- usefulness ----------


def test_usefulness_cold_start_is_half():
    assert usefulness(0, 0) == 0.5


def test_usefulness_rewards_hits():
    # 4 hits out of 6 surfaces → 5/8 = 0.625
    assert usefulness(4, 6) == 0.625


def test_usefulness_never_zero_due_to_smoothing():
    assert usefulness(0, 100) > 0


# ---------- recency ----------


def test_recency_never_surfaced_is_one():
    assert recency(0, 14.0, NOW) == 1.0


def test_recency_decays_over_half_life():
    r = recency(NOW - 14 * DAY, 14.0, NOW)
    assert math.isclose(r, math.exp(-1.0), rel_tol=1e-6)


def test_recency_old_memory_near_zero():
    r = recency(NOW - 365 * DAY, 14.0, NOW)
    assert r < 0.01


# ---------- final_score ----------


def test_final_score_pinned_boosts():
    unpinned = final_score(_candidate(), NOW)
    pinned = final_score(_candidate(pinned=True), NOW)
    assert math.isclose(pinned, unpinned * 1.5)


def test_final_score_useful_memory_beats_fresh():
    fresh = final_score(_candidate(surface_count=0, useful_count=0), NOW)
    proven = final_score(_candidate(surface_count=10, useful_count=8), NOW)
    assert proven > fresh
