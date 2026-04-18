"""Trigger persistence: write FormationCandidates to the triggers table.

Extracted from formation.py to keep that module focused on pure extraction
and annotation. Both dedup.py and remember.py import from here.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .candidates import FormationCandidate


def insert_candidate_triggers(
    conn: sqlite3.Connection,
    memory_id: int,
    candidates: Iterable[FormationCandidate],
) -> int:
    """Write candidates as rows in the triggers table. Returns the insert count."""
    n = 0
    for c in candidates:
        if c.kind == "tool_head":
            head_joined = " ".join(c.head)
            conn.execute(
                "INSERT INTO triggers "
                "(memory_id, kind, tool_name, head_joined, head_length) "
                "VALUES (?, 'tool_head', ?, ?, ?)",
                (memory_id, c.tool_name, head_joined, len(c.head)),
            )
        elif c.kind == "path_glob":
            conn.execute(
                "INSERT INTO triggers (memory_id, kind, path_pattern) "
                "VALUES (?, 'path_glob', ?)",
                (memory_id, c.path_pattern),
            )
        else:
            continue
        n += 1
    return n


def extras_to_candidates(extras: list[dict[str, Any]]) -> list[FormationCandidate]:
    """Convert legacy --extra-trigger dicts into FormationCandidates."""
    return [
        FormationCandidate(
            kind=t["kind"],
            tool_name=t.get("tool_name"),
            head=t.get("head", ()),
            path_pattern=t.get("path_pattern"),
            source="extra",
        )
        for t in extras
    ]
