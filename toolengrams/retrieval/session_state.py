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
    first_token: str | None = None,
) -> None:
    """Insert one session_surfaces row per memory. No-op for empty sessions.

    `first_token` is the tool call's anchor token (e.g. "git" for `git push`).
    Stored on the surface row so PostToolUse can find which prior failure
    surfaces to credit when a same-first_token call succeeds. NULL is fine
    (path-glob-triggered surfaces have no useful first_token).
    """
    if not session_id or not memory_ids:
        return
    rows = [
        (session_id, mid, now_ts, hook, tool_use_id, turn_at_surface, first_token)
        for mid in memory_ids
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface, first_token) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
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


def get_prior_failure_surfaces(
    conn: sqlite3.Connection,
    session_id: str,
    first_token: str,
) -> list[int]:
    """Memory IDs surfaced on a *prior* failed call with the same first_token.

    Returns DISTINCT memory IDs whose post_tool_use_failure surface in this
    session shares `first_token` with the current call AND hasn't already
    been credited (outcome IS NULL). The intent: "Claude saw this hint when
    `git push` failed, then retried `git push` and it worked — credit the
    hint." Empty list if there are no candidates.
    """
    if not session_id or not first_token:
        return []
    rows = conn.execute(
        "SELECT DISTINCT memory_id FROM session_surfaces "
        "WHERE session_id = ? AND hook = 'post_tool_use_failure' "
        "  AND first_token = ? AND outcome IS NULL",
        (session_id, first_token),
    ).fetchall()
    return [r["memory_id"] for r in rows]


def mark_surface_outcome(
    conn: sqlite3.Connection,
    session_id: str,
    memory_ids: Sequence[int],
    outcome: str,
    hook: str | None = None,
    first_token: str | None = None,
) -> int:
    """Set `outcome` on session_surfaces rows for these memories in this session.

    Optional filters (`hook`, `first_token`) narrow the rows. Only rows whose
    outcome is currently NULL are touched — established outcomes don't get
    overwritten. Returns the number of rows updated.
    """
    if not session_id or not memory_ids or outcome not in ("helpful", "unused", "noise"):
        return 0
    placeholders = ",".join("?" * len(memory_ids))
    sql = (
        f"UPDATE session_surfaces SET outcome = ? "
        f"WHERE session_id = ? AND memory_id IN ({placeholders}) "
        f"  AND outcome IS NULL"
    )
    params: list = [outcome, session_id, *memory_ids]
    if hook is not None:
        sql += " AND hook = ?"
        params.append(hook)
    if first_token is not None:
        sql += " AND first_token = ?"
        params.append(first_token)
    cur = conn.execute(sql, params)
    return cur.rowcount or 0


def get_most_recent_unmarked_surface(
    conn: sqlite3.Connection,
    session_id: str,
    memory_id: int,
) -> int | None:
    """surfaced_ts of the latest unmarked surface of this memory in this session.

    Used by `engram skip` to pick which surface row to flag 'unused' when
    Claude rejects a hint after seeing it. Returns None if no unmarked
    surface exists.
    """
    if not session_id:
        return None
    row = conn.execute(
        "SELECT surfaced_ts FROM session_surfaces "
        "WHERE session_id = ? AND memory_id = ? AND outcome IS NULL "
        "ORDER BY surfaced_ts DESC LIMIT 1",
        (session_id, memory_id),
    ).fetchone()
    return row["surfaced_ts"] if row else None


def mark_surface_outcome_by_ts(
    conn: sqlite3.Connection,
    session_id: str,
    memory_id: int,
    surfaced_ts: int,
    outcome: str,
) -> bool:
    """Mark a single specific surface row's outcome. Returns True if updated."""
    if outcome not in ("helpful", "unused", "noise"):
        return False
    cur = conn.execute(
        "UPDATE session_surfaces SET outcome = ? "
        "WHERE session_id = ? AND memory_id = ? AND surfaced_ts = ? "
        "  AND outcome IS NULL",
        (outcome, session_id, memory_id, surfaced_ts),
    )
    return (cur.rowcount or 0) > 0


def find_active_session(conn: sqlite3.Connection, within_seconds: int = 3600) -> str | None:
    """Newest session_id with a surface in the last `within_seconds`. Fallback
    for CLIs (like `engram skip`) when no $CLAUDE_SESSION_ID is set."""
    import time
    cutoff = int(time.time()) - within_seconds
    row = conn.execute(
        "SELECT session_id FROM session_surfaces "
        "WHERE surfaced_ts >= ? "
        "ORDER BY surfaced_ts DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    return row["session_id"] if row else None
