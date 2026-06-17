"""Semantic near-duplicate detection for formation.

`find_overlapping_memory` (dedup.py) catches duplicates that share a trigger in
scope — but a memory worded differently, with different triggers, slips past it
(the live DB had `macos-no-timeout-command` ×3, same name, non-overlapping
triggers, all active). This module is the wider net: it surfaces the top-N
textually-similar existing memories so the formation agent can decide whether it
is about to re-create knowledge that already exists.

Two stages, both stdlib + the existing FTS index (no new dependency):
  1. `memory_store.search` — FTS5/BM25 shortlist over name/description/body
     (cheap, indexed, active-only).
  2. token-Jaccard re-score on name+body — a normalized 0–1 overlap the caller
     can threshold (BM25 `rank` is unbounded and not comparable across queries).
"""

from __future__ import annotations

import re
import sqlite3

from .. import memory_store
from ..models import Memory

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    """|a ∩ b| / |a ∪ b| — 0.0 when either side is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_similar(
    conn: sqlite3.Connection,
    name: str,
    body: str,
    *,
    limit: int = 3,
    exclude_id: int | None = None,
) -> list[tuple[Memory, float]]:
    """Top-`limit` active memories textually similar to (name, body), each with
    its token-Jaccard score, highest first. Empty when nothing matches.

    Searches all scopes deliberately — a mis-scoped project duplicate of a
    global memory (or vice-versa) is exactly a case worth surfacing.
    """
    query = f"{name} {body}".strip()
    if not query:
        return []
    want = _tokens(query)
    # Over-fetch the FTS shortlist, then re-rank by Jaccard and trim.
    candidates = memory_store.search(conn, query, limit=max(limit * 4, limit))
    scored = [
        (m, jaccard(want, _tokens(f"{m.name} {m.body}")))
        for m in candidates
        if exclude_id is None or m.id != exclude_id
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]
