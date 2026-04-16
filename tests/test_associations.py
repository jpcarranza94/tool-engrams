"""Unit tests for Hebbian co-activation: associations.py."""

from __future__ import annotations

import math
import time

from toolengrams.associations import (
    ALPHA,
    ASSOC_BOOST,
    ASSOC_HALF_LIFE_DAYS,
    TAU_TURNS,
    get_prior_surfaces_with_turn,
    get_session_turn,
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

    # One turn apart.
    n = record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m2],
        prior_surfaced={m1: 0},
        current_turn=1,
        now_ts=now_ts,
    )
    assert n == 1

    assoc = _get_assoc(temp_db, m1, m2)
    assert assoc is not None
    expected_signal = math.exp(-1 / TAU_TURNS)  # ≈ 0.82
    expected_strength = ALPHA * expected_signal
    assert abs(assoc["strength"] - expected_strength) < 0.001
    assert assoc["co_fire_count"] == 1


def test_one_turn_apart_signal_is_about_0_82(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m2],
        prior_surfaced={m1: 0},
        current_turn=1,
        now_ts=now_ts,
    )
    assoc = _get_assoc(temp_db, m1, m2)
    # exp(-1/5) ≈ 0.8187 → strength = 0.2 * 0.8187 ≈ 0.164
    assert abs(assoc["strength"] - 0.164) < 0.005


def test_five_turns_apart_signal_is_one_over_e(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m2],
        prior_surfaced={m1: 0},
        current_turn=5,
        now_ts=now_ts,
    )
    assoc = _get_assoc(temp_db, m1, m2)
    expected_signal = math.exp(-1)  # ≈ 0.368
    expected_strength = ALPHA * expected_signal
    assert abs(assoc["strength"] - expected_strength) < 0.005


def test_bounded_growth_never_exceeds_1(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    # Fire together 50 times with max signal (same turn = same tool call).
    for i in range(50):
        record_co_activations(
            temp_db, "sess1",
            newly_surfaced_ids=[m2],
            prior_surfaced={m1: 7},
            current_turn=7,
            now_ts=now_ts,
        )

    assoc = _get_assoc(temp_db, m1, m2)
    assert assoc["strength"] < 1.0
    assert assoc["strength"] > 0.95  # should be close to 1 after 50 fires
    assert assoc["co_fire_count"] == 50


def test_distant_turn_co_fire_below_skip_threshold(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    # 20 turns apart → exp(-20/5) = exp(-4) ≈ 0.018 — still above the 0.01 floor.
    # 25 turns apart → exp(-5) ≈ 0.0067 — below the floor, skipped.
    record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m2],
        prior_surfaced={m1: 0},
        current_turn=25,
        now_ts=now_ts,
    )

    assoc = _get_assoc(temp_db, m1, m2)
    assert assoc is None  # skipped


def test_unknown_turn_priors_skipped(temp_db):
    """prior surfaces from before v3 migration have turn=-1 sentinel."""
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    n = record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m2],
        prior_surfaced={m1: -1},  # sentinel for NULL turn_at_surface
        current_turn=3,
        now_ts=now_ts,
    )
    assert n == 0
    assert _get_assoc(temp_db, m1, m2) is None


def test_newly_surfaced_pair_each_other(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    m3 = _seed(temp_db, "mem3")
    now_ts = int(time.time())

    n = record_co_activations(
        temp_db, "sess1",
        newly_surfaced_ids=[m1, m2, m3],
        prior_surfaced={},
        current_turn=0,
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
        prior_surfaced={m1: 0},
        current_turn=1,
        now_ts=now_ts,
    )
    assert n == 0


# ---------- prior surface + turn lookup ----------


def test_get_prior_surfaces_with_turn_returns_max_turn(temp_db):
    m1 = _seed(temp_db, "mem1")
    m2 = _seed(temp_db, "mem2")
    now_ts = int(time.time())

    temp_db.execute(
        "INSERT INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES ('sess1', ?, ?, 'pre_tool_use', 't1', 2)",
        (m1, now_ts),
    )
    # A second surface for the same memory at a LATER turn — we want max.
    temp_db.execute(
        "INSERT INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES ('sess1', ?, ?, 'pre_tool_use_assoc', 't2', 5)",
        (m1, now_ts + 1),
    )
    temp_db.execute(
        "INSERT INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES ('sess1', ?, ?, 'pre_tool_use', 't3', 3)",
        (m2, now_ts),
    )

    result = get_prior_surfaces_with_turn(temp_db, "sess1")
    assert result[m1] == 5
    assert result[m2] == 3


def test_get_prior_surfaces_null_turn_sentinel(temp_db):
    """Pre-v3 rows have NULL turn_at_surface — map to -1 sentinel."""
    m1 = _seed(temp_db, "mem1")
    now_ts = int(time.time())

    temp_db.execute(
        "INSERT INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES ('sess1', ?, ?, 'pre_tool_use', 't1', NULL)",
        (m1, now_ts),
    )

    result = get_prior_surfaces_with_turn(temp_db, "sess1")
    assert result[m1] == -1


def test_get_session_turn_missing_returns_zero(temp_db):
    assert get_session_turn(temp_db, "nonexistent") == 0


def test_get_session_turn_reads_counter(temp_db):
    now_ts = int(time.time())
    temp_db.execute(
        "INSERT INTO session_turns (session_id, turn_count, updated_ts) "
        "VALUES ('sess1', 7, ?)",
        (now_ts,),
    )
    assert get_session_turn(temp_db, "sess1") == 7

