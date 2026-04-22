"""Candidate retrieval + per-cluster Laplace-smoothed filter.

Matching model (v2, see docs/design-v9.md §3.2):
  - token_subseq: the call's first token selects a bucket (indexed lookup),
    then we subsequence-match the stored trigger tokens against the call's
    tokens in Python.
  - path_glob: fnmatch the stored pattern against each extracted call path.

Scoring formulas live in reinforcement/scoring.py — this module is only about
selecting candidates from the store and gating them by cluster quality.
"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
import time
from dataclasses import dataclass

from ..models import Candidate, ClusterStats
from ..reinforcement.scoring import final_score
from .extract import ExtractedTriggerHint

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


# ---------- candidate retrieval ----------


def retrieve_candidates(
    conn: sqlite3.Connection,
    hint: ExtractedTriggerHint,
    project_slug: str | None,
    now_ts: int,
    kind: str | None = None,
) -> list[Candidate]:
    """Return memories whose triggers match this tool call.

    Scope filter: (scope='global') OR (scope='project' AND project_slug=?).
    Archived memories excluded.

    If `kind` is provided ('block' or 'hint'), only memories of that kind are
    returned — lets pretool ask for blocks, PostToolUseFailure ask for hints.
    """
    candidates: dict[int, Candidate] = {}
    kind_sql = " AND m.kind = ?" if kind else ""
    kind_args = (kind,) if kind else ()

    # --- token_subseq matches ---
    if hint.tokens:
        first = hint.tokens[0]
        sql = f"""
            SELECT
                m.id, m.name, m.body, m.kind, m.scope,
                m.surface_count, m.useful_count, m.last_surfaced_ts, m.pinned,
                t.tokens_json
            FROM triggers t
            JOIN memories m ON m.id = t.memory_id
            WHERE t.kind = 'token_subseq'
              AND t.first_token = ?
              AND m.archived_ts IS NULL
              AND (m.scope = 'global' OR m.project_slug = ?)
              {kind_sql}
        """
        rows = conn.execute(sql, (first, project_slug, *kind_args)).fetchall()
        call = tuple(hint.tokens)
        for row in rows:
            stored = _load_tokens(row["tokens_json"])
            if not stored:
                continue
            if is_subsequence(stored, call):
                _merge_token_candidate(candidates, row, stored)

    # --- path_glob matches ---
    if hint.paths:
        sql = f"""
            SELECT
                m.id, m.name, m.body, m.kind, m.scope,
                m.surface_count, m.useful_count, m.last_surfaced_ts, m.pinned,
                t.path_pattern
            FROM triggers t
            JOIN memories m ON m.id = t.memory_id
            WHERE t.kind = 'path_glob'
              AND m.archived_ts IS NULL
              AND (m.scope = 'global' OR m.project_slug = ?)
              {kind_sql}
        """
        rows = conn.execute(sql, (project_slug, *kind_args)).fetchall()
        for row in rows:
            if _any_path_matches(row["path_pattern"], hint.paths):
                _merge_path_candidate(candidates, row)

    for c in candidates.values():
        c.final_score = final_score(c, now_ts)

    return list(candidates.values())


def is_subsequence(needle: tuple[str, ...], haystack: tuple[str, ...]) -> bool:
    """All tokens in `needle` appear in `haystack` in order (non-contiguous allowed).

    Matches v2 §3.2: `["ergeon", "order", "reassign"]` matches
    `ergeon order 12345 reassign` because "12345" is simply skipped.
    """
    if not needle:
        return False
    it = iter(haystack)
    return all(token in it for token in needle)


def _load_tokens(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(x) for x in parsed)


def _any_path_matches(pattern: str | None, paths: list[str]) -> bool:
    if not pattern:
        return False
    return any(fnmatch.fnmatchcase(p, pattern) for p in paths)


def _merge_token_candidate(
    store: dict[int, Candidate],
    row: sqlite3.Row,
    stored_tokens: tuple[str, ...],
) -> None:
    existing = store.get(row["id"])
    if existing is None or len(stored_tokens) > len(existing.matched_tokens):
        store[row["id"]] = Candidate(
            memory_id=row["id"],
            name=row["name"],
            body=row["body"],
            matched_tokens=stored_tokens,
            matched_path=None,
            surface_count=row["surface_count"],
            useful_count=row["useful_count"],
            last_surfaced_ts=row["last_surfaced_ts"],
            pinned=bool(row["pinned"]),
            kind=row["kind"],
            scope=row["scope"],
        )


def _merge_path_candidate(store: dict[int, Candidate], row: sqlite3.Row) -> None:
    if row["id"] in store:
        return
    store[row["id"]] = Candidate(
        memory_id=row["id"],
        name=row["name"],
        body=row["body"],
        matched_tokens=(),
        matched_path=row["path_pattern"],
        surface_count=row["surface_count"],
        useful_count=row["useful_count"],
        last_surfaced_ts=row["last_surfaced_ts"],
        pinned=bool(row["pinned"]),
        kind=row["kind"],
        scope=row["scope"],
    )


# ---------- cluster stats + Laplace threshold ----------


def compute_cluster_stats(
    conn: sqlite3.Connection,
    project_slug: str | None,
    now_ts: int,
) -> dict[str, ClusterStats]:
    """Aggregate final_score by first_token across non-archived memories.

    Path-glob triggers share the empty-string ('') bucket — they have no
    first_token. This is cheap at v2 scale. If the corpus grows, add a cached
    cluster_stats table.
    """
    sql = """
        SELECT
            t.kind, t.first_token,
            m.kind AS memory_kind, m.surface_count, m.useful_count,
            m.last_surfaced_ts, m.pinned
        FROM triggers t
        JOIN memories m ON m.id = t.memory_id
        WHERE m.archived_ts IS NULL
          AND (m.scope = 'global' OR m.project_slug = ?)
    """
    rows = conn.execute(sql, (project_slug,)).fetchall()
    agg: dict[str, ClusterStats] = {}
    for row in rows:
        key = row["first_token"] or ""
        c = Candidate(
            memory_id=0,
            name="",
            body="",
            matched_tokens=(),
            matched_path=None,
            surface_count=row["surface_count"],
            useful_count=row["useful_count"],
            last_surfaced_ts=row["last_surfaced_ts"],
            pinned=bool(row["pinned"]),
            kind=row["memory_kind"],
            scope="project",
        )
        score = final_score(c, now_ts)
        if key not in agg:
            agg[key] = ClusterStats(
                first_token=key,
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
    cluster_stats: dict[str, ClusterStats],
    surfaced_ids: set[int],
    cfg: FilterConfig | None = None,
) -> list[Candidate]:
    """Apply per-cluster threshold + session dedup, return top-K sorted."""
    cfg = cfg or FilterConfig()
    accepted: list[Candidate] = []
    for c in candidates:
        if c.memory_id in surfaced_ids:
            continue
        cluster = cluster_stats.get(c.first_token)
        threshold = smoothed_threshold(cluster, cfg)
        if c.final_score >= threshold:
            accepted.append(c)
    accepted.sort(key=lambda c: (-len(c.matched_tokens), -c.final_score))
    return accepted[: cfg.top_k]


def now() -> int:
    return int(time.time())
