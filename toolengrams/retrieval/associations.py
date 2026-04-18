"""Hebbian co-activation: memories that fire together wire together.

When two memories surface in the same session, their association strengthens.
When one surfaces later, it boosts the score of its associates.

Operations:
  1. record_co_activations() — called after _log_surfaces in pretool.
  2. retrieve_associates_of() — pretool's associative-track retrieval; walks
     outward from prior session surfaces and returns linked memory ids + boosts.
  3. Bounded Hebbian growth + read-time decay (no background jobs).

Signal distance is measured in *conversational turns* (tool calls), not
wall-clock seconds. A 5-minute gap might be 20 rapid tool calls of dense work
or a single long thinking pause — turn count captures interaction density
better than time.
"""

from __future__ import annotations

import math
import sqlite3
import time


# DEPRECATED: previous time-based signal window, kept for a release so external
# callers don't break. Co-fire formation now uses TAU_TURNS. Safe to remove
# once no migration paths reference it.
TAU_SECONDS = 300.0

# Co-activation window in turns: signal = exp(-Δturns / TAU_TURNS).
# 1 turn ≈ 0.82, 5 turns = 1/e ≈ 0.37, 20 turns ≪ 0.01 (skipped).
TAU_TURNS = 5.0

# Learning rate: new = old + ALPHA * signal * (1 - old). Bounded in [0, 1).
ALPHA = 0.2

# Association half-life in days. Unused associations decay over ~3 months.
# Calendar time IS the right signal for "stale association" — keep days-based.
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
    current_turn: int,
    now_ts: int,
) -> int:
    """Record co-activation between newly surfaced memories and prior surfaces.

    Args:
        newly_surfaced_ids: Memory IDs surfaced on this tool call (post-filter).
        prior_surfaced: {memory_id: turn_at_surface} for memories surfaced
                        earlier in this session (BEFORE this call's log).
                        Entries with turn_at_surface < 0 (unknown) are skipped.
        current_turn: Turn number at which the new memories are surfacing.
        now_ts: Current timestamp (stored on the association as last_co_fire_ts).

    Returns:
        Number of association pairs updated.
    """
    if not newly_surfaced_ids:
        return 0

    # Limit prior surfaces to the MAX_PRIOR_SURFACES closest turns (max turn value
    # = most recent). This bounds work in marathon sessions.
    if prior_surfaced and len(prior_surfaced) > MAX_PRIOR_SURFACES:
        sorted_prior = sorted(prior_surfaced.items(), key=lambda x: x[1], reverse=True)
        prior_surfaced = dict(sorted_prior[:MAX_PRIOR_SURFACES])

    pairs_updated = 0
    for new_id in newly_surfaced_ids:
        for prior_id, prior_turn in prior_surfaced.items():
            if new_id == prior_id:
                continue
            if prior_turn is None or prior_turn < 0:
                # Unknown distance (e.g. pre-v3 surface rows) — skip.
                continue

            dturns = abs(current_turn - prior_turn)
            signal = math.exp(-dturns / TAU_TURNS)
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


def retrieve_associates_of(
    conn: sqlite3.Connection,
    prior_surfaced_ids: set[int],
    exclude_ids: set[int],
    project_slug: str | None,
    now_ts: int,
    min_boost: float,
) -> list[tuple[int, float]]:
    """Find memories linked to prior session surfaces, for the associative track.

    Walks outward from prior_surfaced_ids across memory_associations, decays by
    calendar time, and returns (memory_id, boost) pairs whose effective boost
    meets min_boost. Excludes archived memories, respects project scope, and
    skips any memory_id in exclude_ids (primary selections + already-surfaced).
    """
    if not prior_surfaced_ids:
        return []

    placeholders_p = ",".join("?" * len(prior_surfaced_ids))
    prior_list = list(prior_surfaced_ids)

    rows = conn.execute(
        f"""
        SELECT other_id AS mem_id, strength, last_co_fire_ts
        FROM (
            SELECT memory_b_id AS other_id, strength, last_co_fire_ts
            FROM memory_associations
            WHERE memory_a_id IN ({placeholders_p})
            UNION ALL
            SELECT memory_a_id AS other_id, strength, last_co_fire_ts
            FROM memory_associations
            WHERE memory_b_id IN ({placeholders_p})
        ) AS links
        JOIN memories m ON m.id = links.other_id
        WHERE m.archived_ts IS NULL
          AND (m.scope = 'global' OR m.project_slug = ?)
        """,
        (*prior_list, *prior_list, project_slug),
    ).fetchall()

    # Keep the strongest boost per memory.
    best: dict[int, float] = {}
    for row in rows:
        mem_id = row["mem_id"]
        if mem_id in prior_surfaced_ids or mem_id in exclude_ids:
            continue
        effective = _decayed_strength(row["strength"], row["last_co_fire_ts"], now_ts)
        if effective < 0.01:
            continue
        boost = ASSOC_BOOST * effective
        if boost < min_boost:
            continue
        if mem_id not in best or boost > best[mem_id]:
            best[mem_id] = boost

    return sorted(best.items(), key=lambda x: -x[1])


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
