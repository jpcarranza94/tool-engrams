"""Counter mutations: the side effects that feed the scoring formula.

Every time `memories.surface_count`, `memories.useful_count`,
`memories.last_surfaced_ts`, or `memories.archived_ts` changes, it goes
through one of these helpers. Centralizing the writes means callers
(hooks, CLI, consolidation) share a single definition of what a
"bump", "demote", "archive", or "restore" means.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Sequence

# Soft-demote penalty: add this many phantom surfaces to crater the
# usefulness ratio without fully hiding the memory.
SOFT_DEMOTE_PENALTY = 5


def bump_surface_counts(
    conn: sqlite3.Connection,
    memory_ids: Sequence[int],
    now_ts: int,
) -> None:
    """Increment surface_count and refresh last_surfaced_ts for each memory."""
    if not memory_ids:
        return
    placeholders = ",".join("?" * len(memory_ids))
    conn.execute(
        f"UPDATE memories SET surface_count = surface_count + 1, "
        f"last_surfaced_ts = ? WHERE id IN ({placeholders})",
        (now_ts, *memory_ids),
    )


def bump_useful_counts(
    conn: sqlite3.Connection,
    memory_ids: Sequence[int],
) -> None:
    """Increment useful_count for each memory — success reinforcement."""
    if not memory_ids:
        return
    placeholders = ",".join("?" * len(memory_ids))
    conn.execute(
        f"UPDATE memories SET useful_count = useful_count + 1 "
        f"WHERE id IN ({placeholders})",
        list(memory_ids),
    )


def soft_demote(conn: sqlite3.Connection, memory_id: int) -> None:
    """Crater usefulness without archiving: useful=0, surface_count += penalty."""
    conn.execute(
        "UPDATE memories SET useful_count = 0, "
        "surface_count = surface_count + ?, last_surfaced_ts = 0 "
        "WHERE id = ?",
        (SOFT_DEMOTE_PENALTY, memory_id),
    )


def archive(conn: sqlite3.Connection, memory_id: int, now_ts: int | None = None) -> None:
    """Mark a memory archived. Excluded from retrieval."""
    ts = now_ts if now_ts is not None else int(time.time())
    conn.execute(
        "UPDATE memories SET archived_ts = ? WHERE id = ?",
        (ts, memory_id),
    )


def restore(conn: sqlite3.Connection, memory_id: int) -> None:
    """Undo a soft-demote or archive: clear archive and reset counters to zero."""
    conn.execute(
        "UPDATE memories SET archived_ts = NULL, "
        "useful_count = 0, surface_count = 0, last_surfaced_ts = 0 "
        "WHERE id = ?",
        (memory_id,),
    )
