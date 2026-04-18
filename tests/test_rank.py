"""Reinforcement scoring + Laplace threshold tests."""

from __future__ import annotations

import math

from toolengrams.models import Candidate, ClusterStats
from toolengrams.reinforcement.scoring import final_score, recency, usefulness
from toolengrams.retrieval.rank import (
    FilterConfig,
    filter_candidates,
    smoothed_threshold,
)

NOW = 1_780_000_000  # fixed "now" for deterministic recency math
DAY = 86_400


def _candidate(
    memory_id: int = 1,
    surface_count: int = 0,
    useful_count: int = 0,
    last_surfaced_ts: int = 0,
    pinned: bool = False,
    type_: str = "reference",
    head_length: int = 1,
) -> Candidate:
    return Candidate(
        memory_id=memory_id,
        name=f"m{memory_id}",
        body="body",
        tool_name="Bash",
        head_joined="git",
        head_length=head_length,
        surface_count=surface_count,
        useful_count=useful_count,
        last_surfaced_ts=last_surfaced_ts,
        pinned=pinned,
        type=type_,
        scope="project",
        structural_match=1.0,
    )


# ---------- usefulness ----------


def test_usefulness_cold_start_is_half():
    assert usefulness(0, 0) == 0.5


def test_usefulness_rewards_hits():
    # 4 hits out of 6 surfaces → (5)/(8) = 0.625
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


def test_final_score_ignores_association_boost():
    """Hebbian boost runs on a separate track and must NOT affect primary score."""
    plain = _candidate()
    boosted = _candidate()
    boosted.association_boost = 0.5  # a big boost
    assert math.isclose(final_score(plain, NOW), final_score(boosted, NOW))


# ---------- smoothed_threshold ----------


def test_threshold_cold_cluster_uses_prior():
    cfg = FilterConfig()
    t = smoothed_threshold(None, cfg)
    # With no cluster, smoothed_mean = prior_mean = 0.3; threshold = max(0.3*0.9, 0.15) = 0.27
    assert math.isclose(t, 0.27, rel_tol=1e-6)


def test_threshold_absolute_floor_kicks_in_on_weak_cluster():
    cfg = FilterConfig()
    cluster = ClusterStats(
        tool_name="Bash",
        head_joined="git",
        n_memories=5,
        sum_final_score=0.1,  # average 0.02
    )
    # smoothed_mean = (0.1 + 0.3*3) / (5+3) = 1.0/8 = 0.125; *0.9 = 0.1125; floor pulls up to 0.15
    t = smoothed_threshold(cluster, cfg)
    assert t == 0.15


def test_threshold_mature_cluster_tightens():
    cfg = FilterConfig()
    cluster = ClusterStats(
        tool_name="Bash",
        head_joined="git",
        n_memories=10,
        sum_final_score=5.0,  # average 0.5
    )
    # smoothed_mean = (5 + 0.9) / 13 ≈ 0.4538; *0.9 ≈ 0.4085
    t = smoothed_threshold(cluster, cfg)
    assert 0.40 < t < 0.42


# ---------- filter_candidates ----------


def test_filter_cold_start_new_memory_passes():
    cfg = FilterConfig()
    c = _candidate()
    c.final_score = final_score(c, NOW)
    kept = filter_candidates([c], cluster_stats={}, surfaced_ids=set(), cfg=cfg)
    assert len(kept) == 1


def test_filter_session_dedup():
    cfg = FilterConfig()
    c = _candidate(memory_id=42)
    c.final_score = final_score(c, NOW)
    kept = filter_candidates([c], cluster_stats={}, surfaced_ids={42}, cfg=cfg)
    assert kept == []


def test_filter_weak_memory_in_mature_cluster_gets_filtered():
    cfg = FilterConfig()
    cluster = ClusterStats(
        tool_name="Bash",
        head_joined="git",
        n_memories=10,
        sum_final_score=8.0,  # average 0.8
    )
    weak = _candidate(memory_id=1)
    weak.final_score = 0.3  # below the ~0.63 threshold
    kept = filter_candidates(
        [weak],
        cluster_stats={("Bash", "git"): cluster},
        surfaced_ids=set(),
        cfg=cfg,
    )
    assert kept == []


def test_filter_longer_head_wins_tiebreak():
    cfg = FilterConfig()
    short = _candidate(memory_id=1, head_length=1)
    short.final_score = 0.5
    long_ = _candidate(memory_id=2, head_length=2)
    long_.final_score = 0.5
    kept = filter_candidates([short, long_], cluster_stats={}, surfaced_ids=set(), cfg=cfg)
    assert kept[0].memory_id == 2
    assert kept[1].memory_id == 1
