"""Trigger persistence: write FormationCandidates to the triggers table.

Both dedup.py and cli/remember.py import from here. Storage shape:
  - token_subseq: first_token (indexed) + tokens_json (JSON array of tokens)
  - path_glob: path_pattern
"""

from __future__ import annotations

import json
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
        if c.kind == "token_subseq":
            tokens = tuple(c.tokens)
            if not tokens:
                continue
            conn.execute(
                "INSERT INTO triggers "
                "(memory_id, kind, first_token, tokens_json) "
                "VALUES (?, 'token_subseq', ?, ?)",
                (memory_id, tokens[0], json.dumps(list(tokens))),
            )
        elif c.kind == "path_glob":
            if not c.path_pattern:
                continue
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
    out: list[FormationCandidate] = []
    for t in extras:
        kind = t.get("kind")
        if kind == "token_subseq":
            out.append(FormationCandidate(
                kind="token_subseq",
                tokens=tuple(t.get("tokens") or ()),
                source="extra",
            ))
        elif kind == "path_glob":
            out.append(FormationCandidate(
                kind="path_glob",
                path_pattern=t.get("path_pattern"),
                source="extra",
            ))
    return out
