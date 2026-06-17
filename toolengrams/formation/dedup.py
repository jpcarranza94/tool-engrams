"""Trigger-overlap dedup: detect and update existing memories instead of duplicating.

When `engram remember` is about to insert a new memory, this module checks
whether an existing memory shares triggers. If overlap is strong enough,
the existing memory is updated (body replaced, triggers re-extracted)
instead of creating a duplicate.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from .. import db, memory_store
from .candidates import FormationCandidate
from .triggers import extras_to_candidates, insert_candidate_triggers

# If an existing memory shares this many triggers with the new one,
# update instead of insert.
DEDUP_TRIGGER_THRESHOLD = 1


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def find_overlapping_memory(
    conn: sqlite3.Connection,
    name: str,
    candidates: list[FormationCandidate],
    project_slug: str | None,
) -> dict | None:
    """Find an existing non-archived memory that overlaps with the new one.

    Returns {id, name, overlap_count, match_reason} or None.
    """
    norm_name = normalize_name(name)

    new_tokens: set[str] = set()   # serialized tokens_json strings
    new_globs: set[str] = set()
    for c in candidates:
        if c.kind == "token_subseq" and c.tokens:
            new_tokens.add(json.dumps(list(c.tokens)))
        elif c.kind == "path_glob" and c.path_pattern:
            new_globs.add(c.path_pattern)

    if not new_tokens and not new_globs:
        return None

    rows = memory_store.overlap_rows(conn, project_slug)

    scores: dict[int, dict] = {}
    for row in rows:
        mid = row["id"]
        if mid not in scores:
            scores[mid] = {"id": mid, "name": row["name"], "overlap": 0, "reason": []}

        if row["kind"] == "token_subseq":
            key = row["tokens_json"] or ""
            if key and key in new_tokens:
                scores[mid]["overlap"] += 1
                scores[mid]["reason"].append(f"token_subseq:{key}")
        elif row["kind"] == "path_glob":
            if row["path_pattern"] in new_globs:
                scores[mid]["overlap"] += 1
                scores[mid]["reason"].append(f"path_glob:{row['path_pattern']}")

    best = None
    for s in scores.values():
        if s["overlap"] >= DEDUP_TRIGGER_THRESHOLD:
            if best is None or s["overlap"] > best["overlap"]:
                best = s
        elif s["overlap"] >= 1 and normalize_name(s["name"]) == norm_name:
            s["reason"].append("name_match")
            if best is None or s["overlap"] > best["overlap"]:
                best = s

    if best:
        return {
            "id": best["id"],
            "name": best["name"],
            "overlap_count": best["overlap"],
            "match_reason": ", ".join(best["reason"]),
        }
    return None


def update_existing_memory(
    conn: sqlite3.Connection,
    existing_id: int,
    name: str,
    description: str,
    body: str,
    kind: str,
    pinned: bool,
    candidates: list[FormationCandidate],
    extra_triggers: list[dict[str, Any]],
    origin_session_id: str | None = None,
) -> int:
    """Replace body/name/kind on an existing memory, merge triggers. The origin
    is re-stamped to the UPDATING session (or cleared for manual updates) — the
    replaced body belongs to whoever wrote it now, and that session's echo is
    the one ADR-0006 must suppress."""
    now_ts = int(time.time())
    with db.transaction(conn):
        memory_store.update_memory(
            conn, existing_id, name=name, description=description, body=body,
            kind=kind, pinned=pinned, created_ts=now_ts,
        )
        memory_store.set_origin_session(conn, existing_id, origin_session_id)
        # Only replace triggers when the caller supplied some. A merge (`remember
        # --into <id>`) whose body extracts no triggers must KEEP the target's
        # existing triggers, not wipe them into a never-surfacing memory.
        if candidates or extra_triggers:
            memory_store.delete_triggers_for(conn, existing_id)
            insert_candidate_triggers(conn, existing_id, candidates)
            insert_candidate_triggers(conn, existing_id, extras_to_candidates(extra_triggers))
    return existing_id
