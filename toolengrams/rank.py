"""Reinforcement scoring, per-cluster Laplace-smoothed threshold, candidate retrieval."""

from __future__ import annotations

import fnmatch
import math
import sqlite3
import time
from dataclasses import dataclass

from .extract import ExtractedTriggerHint, join_head
from .models import Candidate, ClusterStats, MemoryType

# Reinforcement constants — how quickly unused memories decay.
# feedback memories decay faster (30 days) because stale corrections are harmful.
# reference memories are more evergreen (60 days).
HALF_LIFE_DAYS: dict[MemoryType, float] = {
    "feedback": 30.0,
    "reference": 60.0,
}

# Laplace threshold constants — control which candidates pass the quality gate.
#
# PRIOR_MEAN: assumed average quality of a memory before any data. Lower values
#   make the gate more permissive (more memories surface). Range: 0.0–1.0.
# PRIOR_WEIGHT: how many "virtual observations" the prior counts for. Higher
#   values make the threshold harder to move with real data. Range: 1.0–10.0.
# CLUSTER_FACTOR: multiplier on the per-cluster smoothed mean to get the
#   threshold. Below 1.0 means "accept slightly below-average memories".
# ABSOLUTE_FLOOR: minimum threshold regardless of cluster stats. Prevents
#   surfacing truly useless memories even in sparse clusters.
# TOP_K: maximum memories surfaced per tool call after filtering.
PRIOR_MEAN = 0.3
PRIOR_WEIGHT = 3.0
CLUSTER_FACTOR = 0.9
ABSOLUTE_FLOOR = 0.15
TOP_K = 3


@dataclass(slots=True)
class FilterConfig:
    prior_mean: float = PRIOR_MEAN
    prior_weight: float = PRIOR_WEIGHT
    cluster_factor: float = CLUSTER_FACTOR
    absolute_floor: float = ABSOLUTE_FLOOR
    top_k: int = TOP_K


# ---------- scoring ----------


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


def final_score(
    candidate: Candidate,
    now_ts: int,
) -> float:
    """Combine structural match with reinforcement-weighted modifiers."""
    u = usefulness(candidate.useful_count, candidate.surface_count)
    half_life = HALF_LIFE_DAYS.get(candidate.type, HALF_LIFE_DAYS["reference"])
    r = recency(candidate.last_surfaced_ts, half_life, now_ts)
    score = candidate.structural_match * (0.5 + u) * (0.5 + 0.5 * r)
    if candidate.pinned:
        score *= 1.5
    # Hebbian association boost (set by pretool before filtering).
    score *= (1.0 + candidate.association_boost)
    return score


# ---------- candidate retrieval ----------


def retrieve_candidates(
    conn: sqlite3.Connection,
    hint: ExtractedTriggerHint,
    project_slug: str | None,
    now_ts: int,
) -> list[Candidate]:
    """Return all memories whose triggers match this tool call.

    Matching rules (v1):
      - tool_head: any stored head that is a prefix of an extracted head
      - path_glob: fnmatch against each extracted path

    Scope filter: (scope='global') OR (scope='project' AND project_slug=?).
    Archived memories excluded.
    """
    candidates: dict[int, Candidate] = {}

    # --- tool_head matches ---
    head_strings = [join_head(h) for h in hint.head_prefixes]
    if head_strings:
        sql = """
            SELECT
                m.id, m.name, m.body, m.type, m.scope,
                m.surface_count, m.useful_count, m.last_surfaced_ts, m.pinned,
                t.tool_name, t.head_joined, t.head_length
            FROM triggers t
            JOIN memories m ON m.id = t.memory_id
            WHERE t.kind = 'tool_head'
              AND t.tool_name = ?
              AND m.archived_ts IS NULL
              AND (m.scope = 'global' OR m.project_slug = ?)
        """
        rows = conn.execute(sql, (hint.tool_name, project_slug)).fetchall()
        for row in rows:
            if not _head_matches(row["head_joined"], head_strings):
                continue
            _merge_candidate(candidates, row)

    # --- path_glob matches ---
    if hint.paths:
        sql = """
            SELECT
                m.id, m.name, m.body, m.type, m.scope,
                m.surface_count, m.useful_count, m.last_surfaced_ts, m.pinned,
                t.tool_name, t.path_pattern
            FROM triggers t
            JOIN memories m ON m.id = t.memory_id
            WHERE t.kind = 'path_glob'
              AND m.archived_ts IS NULL
              AND (m.scope = 'global' OR m.project_slug = ?)
        """
        rows = conn.execute(sql, (project_slug,)).fetchall()
        for row in rows:
            if not _any_path_matches(row["path_pattern"], hint.paths):
                continue
            _merge_path_candidate(candidates, row)

    # --- score everything ---
    for c in candidates.values():
        c.final_score = final_score(c, now_ts)

    return list(candidates.values())


def _head_matches(stored_head: str | None, call_heads: list[str]) -> bool:
    """Stored head must be a space-joined prefix of at least one of the extracted heads.

    'git' matches 'git push'. 'ssh deploy@' matches 'ssh deploy@35.1.2.3'
    (prefix match on the joined string, not on tokens alone).
    """
    if not stored_head:
        return False
    for call in call_heads:
        if call == stored_head or call.startswith(stored_head + " ") or call.startswith(stored_head):
            # Exact token equality OR token-boundary prefix OR raw string prefix
            # (the last case handles 'ssh deploy@' -> 'ssh deploy@35.1.2.3')
            if _is_valid_prefix(stored_head, call):
                return True
    return False


def _is_valid_prefix(stored: str, call: str) -> bool:
    """Either a token-boundary match or a raw string prefix of the call."""
    if stored == call:
        return True
    if call.startswith(stored + " "):
        return True
    # Raw string prefix — only valid when the stored head already ends in a
    # partial token (no trailing space). Catches 'ssh deploy@' -> 'ssh deploy@35.1.2.3'.
    return call.startswith(stored) and not stored.endswith(" ")


def _any_path_matches(pattern: str | None, paths: list[str]) -> bool:
    if not pattern:
        return False
    return any(fnmatch.fnmatchcase(p, pattern) for p in paths)


def _merge_candidate(store: dict[int, Candidate], row: sqlite3.Row) -> None:
    existing = store.get(row["id"])
    head_length = row["head_length"] or 0
    if existing is None:
        store[row["id"]] = Candidate(
            memory_id=row["id"],
            name=row["name"],
            body=row["body"],
            tool_name=row["tool_name"],
            head_joined=row["head_joined"],
            head_length=head_length,
            surface_count=row["surface_count"],
            useful_count=row["useful_count"],
            last_surfaced_ts=row["last_surfaced_ts"],
            pinned=bool(row["pinned"]),
            type=row["type"],
            scope=row["scope"],
        )
    elif head_length > existing.head_length:
        existing.head_joined = row["head_joined"]
        existing.head_length = head_length


def _merge_path_candidate(store: dict[int, Candidate], row: sqlite3.Row) -> None:
    if row["id"] in store:
        return
    store[row["id"]] = Candidate(
        memory_id=row["id"],
        name=row["name"],
        body=row["body"],
        tool_name=row["tool_name"],
        head_joined=None,
        head_length=0,
        surface_count=row["surface_count"],
        useful_count=row["useful_count"],
        last_surfaced_ts=row["last_surfaced_ts"],
        pinned=bool(row["pinned"]),
        type=row["type"],
        scope=row["scope"],
    )


# ---------- cluster stats + Laplace threshold ----------


def compute_cluster_stats(
    conn: sqlite3.Connection,
    project_slug: str | None,
    now_ts: int,
) -> dict[tuple[str, str], ClusterStats]:
    """Aggregate final_score by (tool_name, head_joined) across non-archived memories.

    This is cheap at v1 scale. If the corpus grows, add a cached cluster_stats table.
    """
    sql = """
        SELECT
            t.tool_name, t.head_joined,
            m.type, m.surface_count, m.useful_count, m.last_surfaced_ts, m.pinned
        FROM triggers t
        JOIN memories m ON m.id = t.memory_id
        WHERE t.kind = 'tool_head'
          AND m.archived_ts IS NULL
          AND (m.scope = 'global' OR m.project_slug = ?)
    """
    rows = conn.execute(sql, (project_slug,)).fetchall()
    agg: dict[tuple[str, str], ClusterStats] = {}
    for row in rows:
        key = (row["tool_name"], row["head_joined"])
        c = Candidate(
            memory_id=0,
            name="",
            body="",
            tool_name=row["tool_name"],
            head_joined=row["head_joined"],
            head_length=0,
            surface_count=row["surface_count"],
            useful_count=row["useful_count"],
            last_surfaced_ts=row["last_surfaced_ts"],
            pinned=bool(row["pinned"]),
            type=row["type"],
            scope="project",
        )
        score = final_score(c, now_ts)
        if key not in agg:
            agg[key] = ClusterStats(
                tool_name=row["tool_name"],
                head_joined=row["head_joined"],
                n_memories=0,
                sum_final_score=0.0,
            )
        agg[key].n_memories += 1
        agg[key].sum_final_score += score
    return agg


def smoothed_threshold(
    cluster: ClusterStats | None,
    cfg: FilterConfig,
) -> float:
    """Laplace-smoothed per-cluster threshold with an absolute floor."""
    n = cluster.n_memories if cluster else 0
    s = cluster.sum_final_score if cluster else 0.0
    smoothed_mean = (s + cfg.prior_mean * cfg.prior_weight) / (n + cfg.prior_weight)
    return max(smoothed_mean * cfg.cluster_factor, cfg.absolute_floor)


def filter_candidates(
    candidates: list[Candidate],
    cluster_stats: dict[tuple[str, str], ClusterStats],
    surfaced_ids: set[int],
    cfg: FilterConfig | None = None,
) -> list[Candidate]:
    """Apply per-cluster threshold + session dedup, return top-K sorted."""
    cfg = cfg or FilterConfig()
    accepted: list[Candidate] = []
    for c in candidates:
        if c.memory_id in surfaced_ids:
            continue
        key = (c.tool_name or "", c.head_joined or "")
        cluster = cluster_stats.get(key)
        threshold = smoothed_threshold(cluster, cfg)
        if c.final_score >= threshold:
            accepted.append(c)
    accepted.sort(key=lambda c: (-c.head_length, -c.final_score))
    return accepted[: cfg.top_k]


def now() -> int:
    return int(time.time())
