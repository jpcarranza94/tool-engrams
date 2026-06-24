"""Persistence seam for the `consolidation_runs` table.

One row per nightly consolidation run: when it ran, what it scanned, and the
metrics the agent reported. Every SQL statement against `consolidation_runs`
lives here; callers (the consolidate runner, `engram status`, the dashboard)
go through these functions. Reads return raw rows for display — a run is
recorded once and rendered, never passed around as a mutable domain object.
"""

from __future__ import annotations

import sqlite3


def was_run(conn: sqlite3.Connection, run_date: str) -> bool:
    """True if a consolidation run is already recorded for this date (the
    idempotency guard; --force bypasses the caller's check)."""
    return conn.execute(
        "SELECT 1 FROM consolidation_runs WHERE run_date = ? LIMIT 1", (run_date,)
    ).fetchone() is not None


def record_run(
    conn: sqlite3.Connection,
    *,
    run_date: str,
    started_ts: int,
    completed_ts: int,
    sessions_scanned: int,
    episodes_evaluated: int,
    memories_weakened: int,
    memories_archived: int,
    memories_discovered: int,
    report: str | None,
    quality_score,
    surfaces_helpful: int,
    surfaces_noise: int,
    memories_verified: int,
    memories_strengthened: int = 0,
) -> None:
    """Upsert the row for `run_date` (INSERT OR REPLACE — a --force re-run
    overwrites the prior record for that date)."""
    conn.execute(
        "INSERT OR REPLACE INTO consolidation_runs "
        "(run_date, started_ts, completed_ts, sessions_scanned, episodes_evaluated, "
        " memories_strengthened, memories_weakened, memories_archived, memories_discovered, "
        " report, quality_score, surfaces_helpful, surfaces_noise, memories_verified) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_date, started_ts, completed_ts, sessions_scanned, episodes_evaluated,
         memories_strengthened, memories_weakened, memories_archived, memories_discovered,
         report, quality_score, surfaces_helpful, surfaces_noise, memories_verified),
    )


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent run's summary fields (engram status)."""
    return conn.execute(
        "SELECT run_date, sessions_scanned, memories_archived, "
        "memories_discovered, memories_strengthened, memories_weakened "
        "FROM consolidation_runs ORDER BY started_ts DESC LIMIT 1"
    ).fetchone()


def recent_runs(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """The most recent runs, newest first, with the full metric set (dashboard)."""
    return conn.execute(
        "SELECT run_date, sessions_scanned, memories_archived, memories_discovered, "
        "memories_strengthened, memories_weakened, "
        "quality_score, surfaces_helpful, surfaces_noise, episodes_evaluated, report "
        "FROM consolidation_runs ORDER BY started_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
