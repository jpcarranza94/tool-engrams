"""Retrieval tests: subsequence match + scoring primitives (q, gate, final_score).

One ratio `q` (noise-aware, Laplace-smoothed) drives ranking; the surfacing
gate suppresses hints proven more noise than signal.
"""

from __future__ import annotations

import math

from toolengrams.models import Candidate
from toolengrams.reinforcement.scoring import final_score, is_gated, q
from toolengrams.retrieval.rank import is_subsequence


def _candidate(
    memory_id: int = 1,
    surface_count: int = 0,
    useful_count: int = 0,
    noise_count: int = 0,
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
        noise_count=noise_count,
        last_surfaced_ts=0,
        pinned=pinned,
        kind=kind,
        scope="project",
    )


# ---------- subsequence match ----------


def test_subseq_exact_match():
    assert is_subsequence(("git", "push"), ("git", "push"))


def test_subseq_with_gap():
    # The poster child: `mycli order 12345 reassign` matches `[mycli, order, reassign]`.
    assert is_subsequence(
        ("mycli", "order", "reassign"),
        ("mycli", "order", "12345", "reassign"),
    )


def test_subseq_wrong_order_fails():
    assert not is_subsequence(
        ("mycli", "order", "reassign"),
        ("mycli", "reassign", "order"),
    )


def test_subseq_missing_token_fails():
    assert not is_subsequence(
        ("mycli", "order", "reassign"),
        ("mycli", "customer", "reassign"),
    )


def test_subseq_empty_needle_fails():
    # A trigger with no tokens is meaningless; treat as no-match.
    assert not is_subsequence((), ("git", "push"))


# ---------- q (noise-aware usefulness) ----------


def test_q_cold_start_is_half():
    assert q(0, 0) == 0.5


def test_q_rewards_helpful_over_noise():
    # 4 helpful, 0 noise → 5/6 ≈ 0.833
    assert math.isclose(q(4, 0), 5 / 6)


def test_q_punished_by_noise():
    # 0 helpful, 4 noise → 1/6 ≈ 0.167 (below the 0.5 prior)
    assert q(0, 4) < 0.5


def test_q_never_zero_due_to_smoothing():
    assert q(0, 100) > 0


def test_q_ignores_unused_surfaces():
    # A situational memory: never judged helpful or noise → stays at the prior.
    assert q(0, 0) == 0.5


# ---------- final_score ----------


def test_final_score_pinned_boosts():
    unpinned = final_score(_candidate())
    pinned = final_score(_candidate(pinned=True))
    assert math.isclose(pinned, unpinned * 1.5)


def test_final_score_helpful_memory_beats_fresh():
    fresh = final_score(_candidate(useful_count=0, noise_count=0))
    proven = final_score(_candidate(useful_count=8, noise_count=0))
    assert proven > fresh


def test_final_score_noisy_memory_below_fresh():
    fresh = final_score(_candidate(useful_count=0, noise_count=0))
    noisy = final_score(_candidate(useful_count=0, noise_count=8))
    assert noisy < fresh


# ---------- surfacing gate ----------


def test_gate_lets_fresh_hint_through():
    assert not is_gated(_candidate(useful_count=0, noise_count=0))


def test_gate_suppresses_noisy_hint_after_warmup():
    # 0 helpful, 3 noise → q = 1/5 = 0.2 < 0.5, and judged = 3 ≥ WARMUP_N.
    assert is_gated(_candidate(kind="hint", useful_count=0, noise_count=3))


def test_gate_holds_fire_below_warmup():
    # 0 helpful, 2 noise → q < 0.5 but only 2 verdicts (< WARMUP_N) → not gated.
    assert not is_gated(_candidate(kind="hint", useful_count=0, noise_count=2))


def test_gate_lets_proven_hint_through():
    # 3 helpful, 0 noise → q = 0.8 ≥ 0.5.
    assert not is_gated(_candidate(kind="hint", useful_count=3, noise_count=0))


def test_gate_threshold_is_exclusive():
    # 2 helpful, 2 noise → q = 3/6 = 0.5 exactly; gate is strict < 0.5 → not gated.
    assert not is_gated(_candidate(kind="hint", useful_count=2, noise_count=2))


def test_gate_exempts_block_below_block_warmup():
    # A block with terrible q but only 10 verdicts (< BLOCK_GATE_WARMUP=12) still
    # fires — a rare-but-correct safety DENY isn't suppressed on thin evidence.
    assert not is_gated(_candidate(kind="block", useful_count=0, noise_count=10))


def test_gate_suppresses_strongly_negative_block_past_warmup():
    # 4 useful, 10 noise → judged=14 ≥ 12 and q = 5/16 ≈ 0.31 < 0.35 floor.
    # The WS3.2 loophole: a heavily-observed, net-negative block IS now gated.
    assert is_gated(_candidate(kind="block", useful_count=4, noise_count=10))


def test_gate_holds_block_at_warmup_boundary():
    # judged = 11 < 12 → still exempt even though q is terrible (safety margin).
    assert not is_gated(_candidate(kind="block", useful_count=0, noise_count=11))


def test_gate_lets_marginal_block_through_above_floor():
    # judged = 20 (past warm-up) but q = 9/22 ≈ 0.409 ≥ 0.35 floor → still fires.
    # Only *strongly* net-negative blocks gate; a middling one keeps its DENY.
    assert not is_gated(_candidate(kind="block", useful_count=8, noise_count=12))


def test_gate_block_at_exact_warmup_boundary_is_gateable():
    # judged = 12 == BLOCK_GATE_WARMUP: the warm-up bar is inclusive (judged <
    # warmup exempts, so exactly-12 is NOT exempt). q = 1/14 ≈ 0.07 < 0.35 → gated.
    # Pins the `<` in the warm-up check against a `<=` mutation that flips at 12.
    assert is_gated(_candidate(kind="block", useful_count=0, noise_count=12))


def test_gate_block_floor_is_exclusive():
    # judged = 18 ≥ warm-up, q = 7/20 = 0.35 exactly; the floor is strict < → NOT
    # gated. Pins the `<` in the floor check against a `<=` mutation.
    assert not is_gated(_candidate(kind="block", useful_count=6, noise_count=12))


def test_gate_exempts_pinned():
    assert not is_gated(_candidate(kind="hint", pinned=True, useful_count=0, noise_count=10))


def test_gate_exempts_pinned_block_even_when_strongly_negative():
    # pinned wins over the block gate: a pinned memory is never suppressed.
    assert not is_gated(
        _candidate(kind="block", pinned=True, useful_count=4, noise_count=10)
    )
