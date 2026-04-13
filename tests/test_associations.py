"""Unit tests for Hebbian co-activation: associations.py."""

from __future__ import annotations

import math
import time

from toolengrams.associations import (
    ALPHA,
    ASSOC_BOOST,
    ASSOC_HALF_LIFE_DAYS,
    TAU_SECONDS,
    lookup_association_boosts,
    record_co_activations,
)


def _seed(conn, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO memories (name, body, type, scope, created_ts) "
        "VALUES (?, 'body', 'reference', 'global', ?)",
        (name, int(time.time())),
    )
    return cur.lastrowid


def _get_assoc(conn, a_id: int, b_id: int) -> dict | None:
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    row = conn.execute(
        "SELECT * FROM memory_associations WHERE memory_a_id = ? AND memory_b_id = ?",
        (lo, hi),
    ).fetchone()
    return dict(row) if row else None


# ---------- co-activation recording ----------


def test_co_activation_creates_association(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    n = record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m2],
        prior_surfaced={m1: now_ts - 10},  # 10 seconds ago
        now_ts=now_ts,
    )
    assert n == 1

    assoc = _get_assoc(temp_db, m1, m2)
    assert assoc is not None
    expected_signal = math.exp(-10 / TAU_SECONDS)
    expected_strength = ALPHA * expected_signal
    assert abs(assoc["strength"] - expected_strength) < 0.001
    assert assoc["co_fire_count"] == 1


def test_bounded_growth_never_exceeds_1(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    # Fire together 50 times with max signal (same tool call).
    for i in range(50):
        record_co_activations(
            temp_db, "sess1",
            newly_surfaced_ids=[m2],
            prior_surfaced={m1: now_ts},
            now_ts=now_ts,
        )

    assoc = _get_assoc(temp_db, m1, m2)
    assert assoc["strength"] < 1.0
    assert assoc["strength"] > 0.95  # should be close to 1 after 50 fires
    assert assoc["co_fire_count"] == 50


def test_distant_co_fire_produces_weak_signal(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m2],
        prior_surfaced={m1: now_ts - 900},  # 15 minutes ago
        now_ts=now_ts,
    )

    assoc = _get_assoc(temp_db, m1, m2)
    # exp(-900/300) = exp(-3) ≈ 0.05 → strength = 0.2 * 0.05 = 0.01
    assert assoc["strength"] < 0.02


def test_negligible_signal_skipped(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m2],
        prior_surfaced={m1: now_ts - 3000},  # 50 minutes ago → signal < 0.01
        now_ts=now_ts,
    )

    assoc = _get_assoc(temp_db, m1, m2)
    assert assoc is None  # skipped


def test_newly_surfaced_pair_each_other(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    m3 = _seed(temp_db, "mem3")
    now_ts = int(time.time())

    n = record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m1, m2, m3],
        prior_surfaced={},
        now_ts=now_ts,
    )
    # C(3,2) = 3 pairs among newly surfaced
    assert n == 3

    assert _get_assoc(temp_db, m1, m2) is not None
    assert _get_assoc(temp_db, m1, m3) is not None
    assert _get_assoc(temp_db, m2, m3) is not None


def test_self_pairing_skipped(temp_db):
    m1 = _seed(temp_db, "mem1")
    now_ts = int(time.time())

    n = record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m1],
        prior_surfaced={m1: now_ts},
        now_ts=now_ts,
    )
    assert n == 0


# ---------- boost lookup ----------


def test_boost_lookup_returns_max_strength(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    m3 = _seed(temp_db, "mem3")
    now_ts = int(time.time())

    # m1↔m2 strong, m1↔m3 weak
    temp_db.execute(
        "INSERT INTO memory_associations (memory_a_id, memory_b_id, strength, co_fire_count, last_co_fire_ts, created_ts) "
        "VALUES (?, ?, 0.8, 5, ?, ?)",
        (m1, m2, now_ts, now_ts),
    )
    temp_db.execute(
        "INSERT INTO memory_associations (memory_a_id, memory_b_id, strength, co_fire_count, last_co_fire_ts, created_ts) "
        "VALUES (?, ?, 0.2, 2, ?, ?)",
        (m1, m3, now_ts, now_ts),
    )

    # Candidate is m1, prior surfaced are m2 and m3
    boosts = lookup_association_boosts(
        temp_db,
        candidate_ids=[m1],
        prior_surfaced_ids={m2, m3},
        now_ts=now_ts,
    )

    # Should use max (m1↔m2 = 0.8), not sum
    assert m1 in boosts
    expected = ASSOC_BOOST * 0.8  # 0.3 * 0.8 = 0.24
    assert abs(boosts[m1] - expected) < 0.01


def test_boost_decays_with_time(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())
    old_ts = now_ts - int(ASSOC_HALF_LIFE_DAYS * 86400)  # one half-life ago

    temp_db.execute(
        "INSERT INTO memory_associations (memory_a_id, memory_b_id, strength, co_fire_count, last_co_fire_ts, created_ts) "
        "VALUES (?, ?, 0.8, 5, ?, ?)",
        (m1, m2, old_ts, old_ts),
    )

    boosts = lookup_association_boosts(
        temp_db,
        candidate_ids=[m1],
        prior_surfaced_ids={m2},
        now_ts=now_ts,
    )

    # After one half-life, effective = 0.8 * 0.5 = 0.4
    expected = ASSOC_BOOST * 0.4  # 0.3 * 0.4 = 0.12
    assert m1 in boosts
    assert abs(boosts[m1] - expected) < 0.02


def test_no_prior_surfaces_no_boosts(temp_db):
    m1 = _seed(temp_db, "mem1")
    boosts = lookup_association_boosts(
        temp_db,
        candidate_ids=[m1],
        prior_surfaced_ids=set(),
        now_ts=int(time.time()),
    )
    assert boosts == {}


def test_symmetric_lookup_works(temp_db):
    """Association stored as (1,2) should be found when candidate=2, prior=1."""
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    # Stored with canonical ordering (m1 < m2)
    temp_db.execute(
        "INSERT INTO memory_associations (memory_a_id, memory_b_id, strength, co_fire_count, last_co_fire_ts, created_ts) "
        "VALUES (?, ?, 0.6, 3, ?, ?)",
        (m1, m2, now_ts, now_ts),
    )

    # Look up with m2 as candidate and m1 as prior (reversed)
    boosts = lookup_association_boosts(
        temp_db,
        candidate_ids=[m2],
        prior_surfaced_ids={m1},
        now_ts=now_ts,
    )
    assert m2 in boosts
    expected = ASSOC_BOOST * 0.6
    assert abs(boosts[m2] - expected) < 0.01
