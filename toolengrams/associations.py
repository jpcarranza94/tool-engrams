"""Hebbian co-activation: memories that fire together wire together.

When two memories surface in the same session, their association strengthens.
When one surfaces later, it boosts the score of its associates.

Three operations:
  1. record_co_activations() — called after _log_surfaces in pretool
  2. lookup_association_boosts() — called before filter_candidates in pretool
  3. Bounded Hebbian growth + read-time decay (no background jobs)
"""

from __future__ import annotations

import math
import sqlite3
import time


# Co-activation window: signal = exp(-Δt / TAU). 5 min = 0.37, 15 min = 0.05.
TAU_SECONDS = 300.0

# Learning rate: new = old + ALPHA * signal * (1 - old). Bounded in [0, 1).
ALPHA = 0.2

# Association half-life in days. Unused associations decay over ~3 months.
ASSOC_HALF_LIFE_DAYS = 90.0

# Max score boost from associations: score *= (1 + ASSOC_BOOST * max_strength).
ASSOC_BOOST = 0.3

# Cap co-activation pairs per call to bound worst-case in marathon sessions.
MAX_PRIOR_SURFACES = 50


def record_co_activations(
    conn: sqlite3.Connection,
    session_id: str,
    newly_surfaced_ids: list[int],
    prior_surfaced: dict[int, int],
    now_ts: int,
) -> int:
    """Record co-activation between newly surfaced memories and prior surfaces.

    Args:
        newly_surfaced_ids: Memory IDs surfaced on this tool call (post-filter).
        prior_surfaced: {memory_id: surfaced_ts} for memories surfaced earlier
                        in this session (BEFORE this call's log).
        now_ts: Current timestamp.

    Returns:
        Number of association pairs updated.
    """
    if not newly_surfaced_ids:
        return 0

    # Limit prior surfaces to most recent MAX_PRIOR_SURFACES to bound work.
    if prior_surfaced and len(prior_surfaced) > MAX_PRIOR_SURFACES:
        sorted_prior = sorted(prior_surfaced.items(), key=lambda x: x[1], reverse=True)
        prior_surfaced = dict(sorted_prior[:MAX_PRIOR_SURFACES])

    pairs_updated = 0
    for new_id in newly_surfaced_ids:
        for prior_id, prior_ts in prior_surfaced.items():
            if new_id == prior_id:
                continue

            dt = abs(now_ts - prior_ts)
            signal = math.exp(-dt / TAU_SECONDS)
            if signal < 0.01:
                continue  # negligible — skip the write

            a_id, b_id = _canonical(new_id, prior_id)
            _upsert_association(conn, a_id, b_id, signal, now_ts)
            pairs_updated += 1

    # Also pair newly surfaced memories with each other (same tool call = max signal).
    for i, id_a in enumerate(newly_surfaced_ids):
        for id_b in newly_surfaced_ids[i + 1:]:
            a_id, b_id = _canonical(id_a, id_b)
            _upsert_association(conn, a_id, b_id, 1.0, now_ts)
            pairs_updated += 1

    return pairs_updated


def lookup_association_boosts(
    conn: sqlite3.Connection,
    candidate_ids: list[int],
    prior_surfaced_ids: set[int],
    now_ts: int,
) -> dict[int, float]:
    """For each candidate, find its max effective association strength
    with any previously-surfaced memory.

    Returns {memory_id: boost_factor} where boost = ASSOC_BOOST * max_strength.
    Missing entries mean no boost (0.0).
    """
    if not candidate_ids or not prior_surfaced_ids:
        return {}

    # Build a single query using UNION for symmetric lookup.
    placeholders_c = ",".join("?" * len(candidate_ids))
    placeholders_p = ",".join("?" * len(prior_surfaced_ids))
    prior_list = list(prior_surfaced_ids)

    rows = conn.execute(
        f"""
        SELECT memory_a_id AS cand, memory_b_id AS prior_id, strength, last_co_fire_ts
        FROM memory_associations
        WHERE memory_a_id IN ({placeholders_c}) AND memory_b_id IN ({placeholders_p})
        UNION ALL
        SELECT memory_b_id AS cand, memory_a_id AS prior_id, strength, last_co_fire_ts
        FROM memory_associations
        WHERE memory_b_id IN ({placeholders_c}) AND memory_a_id IN ({placeholders_p})
        """,
        (*candidate_ids, *prior_list, *candidate_ids, *prior_list),
    ).fetchall()

    # For each candidate, take the max effective (decayed) strength.
    boosts: dict[int, float] = {}
    for row in rows:
        cand_id = row["cand"]
        raw_strength = row["strength"]
        last_ts = row["last_co_fire_ts"]

        effective = _decayed_strength(raw_strength, last_ts, now_ts)
        if effective < 0.01:
            continue

        boost = ASSOC_BOOST * effective
        if cand_id not in boosts or boost > boosts[cand_id]:
            boosts[cand_id] = boost

    return boosts


def get_prior_surfaces_with_ts(
    conn: sqlite3.Connection,
    session_id: str,
) -> dict[int, int]:
    """Get {memory_id: max(surfaced_ts)} for all surfaces in this session so far.

    Used to compute co-activation signal strength based on temporal distance.
    """
    if not session_id:
        return {}
    rows = conn.execute(
        "SELECT memory_id, MAX(surfaced_ts) AS ts "
        "FROM session_surfaces WHERE session_id = ? "
        "GROUP BY memory_id",
        (session_id,),
    ).fetchall()
    return {r["memory_id"]: r["ts"] for r in rows}


# ---------- internals ----------


def _canonical(a: int, b: int) -> tuple[int, int]:
    """Ensure a < b for symmetric storage."""
    return (a, b) if a < b else (b, a)


def _decayed_strength(raw: float, last_ts: int, now_ts: int) -> float:
    """Apply exponential decay to stored strength."""
    if last_ts == 0 or raw <= 0:
        return 0.0
    days = (now_ts - last_ts) / 86400.0
    if days < 0:
        days = 0.0
    return raw * math.exp(-days * math.log(2) / ASSOC_HALF_LIFE_DAYS)


def _upsert_association(
    conn: sqlite3.Connection,
    a_id: int,
    b_id: int,
    signal: float,
    now_ts: int,
) -> None:
    """Bounded Hebbian update: new = old + α × signal × (1 - old).

    Uses INSERT ... ON CONFLICT UPDATE for atomic upsert.
    """
    conn.execute(
        """
        INSERT INTO memory_associations
            (memory_a_id, memory_b_id, strength, co_fire_count, last_co_fire_ts, created_ts)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(memory_a_id, memory_b_id) DO UPDATE SET
            strength = strength + ? * ? * (1.0 - strength),
            co_fire_count = co_fire_count + 1,
            last_co_fire_ts = ?
        """,
        (a_id, b_id, ALPHA * signal, now_ts, now_ts,
         ALPHA, signal, now_ts),
    )
