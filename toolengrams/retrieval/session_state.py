"""Session-scoped state: session_surfaces + session_turns read/write.

Two tables, one concern: tracking what happened in a single Claude Code session.

  - session_surfaces: which memories surfaced, when, under which hook, at which turn.
    Read by pretool (dedup) and post_tool (reinforcement targets).
  - session_turns: monotonic tool-call counter per session.
"""

from __future__ import annotations

import sqlite3
from typing import Sequence


def get_already_surfaced(conn: sqlite3.Connection, session_id: str) -> set[int]:
    """Memory IDs that surfaced at any point in this session."""
    if not session_id:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT memory_id FROM session_surfaces WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return {r["memory_id"] for r in rows}


def log_surfaces(
    conn: sqlite3.Connection,
    session_id: str,
    memory_ids: Sequence[int],
    tool_use_id: str | None,
    hook: str,
    turn_at_surface: int,
    now_ts: int,
) -> None:
    """Insert one session_surfaces row per memory. No-op for empty sessions."""
    if not session_id or not memory_ids:
        return
    rows = [
        (session_id, mid, now_ts, hook, tool_use_id, turn_at_surface)
        for mid in memory_ids
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def get_session_turn(conn: sqlite3.Connection, session_id: str) -> int:
    """Current turn counter for this session, or 0 if not yet seen."""
    if not session_id:
        return 0
    row = conn.execute(
        "SELECT turn_count FROM session_turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row["turn_count"] if row else 0


def increment_session_turn(
    conn: sqlite3.Connection,
    session_id: str,
    now_ts: int,
) -> None:
    """Bump the per-session turn counter. Called once per PostToolUse."""
    conn.execute(
        "INSERT INTO session_turns (session_id, turn_count, updated_ts) "
        "VALUES (?, 1, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "turn_count = turn_count + 1, updated_ts = ?",
        (session_id, now_ts, now_ts),
    )


def get_tool_call_surfaces(
    conn: sqlite3.Connection,
    session_id: str,
    tool_use_id: str,
    hook: str,
) -> list[int]:
    """Memory IDs surfaced by a specific tool_use_id under a specific hook."""
    rows = conn.execute(
        "SELECT memory_id FROM session_surfaces "
        "WHERE session_id = ? AND tool_use_id = ? AND hook = ?",
        (session_id, tool_use_id, hook),
    ).fetchall()
    return [r["memory_id"] for r in rows]
