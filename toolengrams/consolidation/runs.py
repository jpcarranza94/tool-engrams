"""Persistence seam for the `consolidation_runs` table (and its child
`consolidation_recommendations`).

One row per nightly consolidation run: when it ran, what it scanned, and the
metrics the agent reported. Every SQL statement against `consolidation_runs`
and `consolidation_recommendations` lives here; callers (the consolidate
runner, `engram status`, the dashboard) go through these functions. Reads
return raw rows for display — a run is recorded once and rendered, never
passed around as a mutable domain object.
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


def insert_recommendations(
    conn: sqlite3.Connection,
    run_date: str,
    recommendations: list[dict],
    *,
    now_ts: int,
) -> None:
    """Replace the recommendation set for `run_date` (delete-then-insert).

    Mirrors `record_run`'s INSERT OR REPLACE semantics: a --force re-run of a day
    overwrites that day's recommendations wholesale, so the table never
    accumulates duplicates from re-runs. Each item is a validated dict with keys
    title, severity, status, detail, issue_url (the caller normalizes the vocab
    and drops malformed entries). `resolved_ts` is stamped now for items the
    agent already marked `done`, NULL otherwise.
    """
    conn.execute(
        "DELETE FROM consolidation_recommendations WHERE run_date = ?", (run_date,)
    )
    conn.executemany(
        "INSERT INTO consolidation_recommendations "
        "(run_date, title, severity, status, detail, issue_url, created_ts, resolved_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (run_date, r["title"], r["severity"], r["status"], r.get("detail"),
             r.get("issue_url"), now_ts,
             now_ts if r["status"] == "done" else None)
            for r in recommendations
        ],
    )


def recommendations_across_runs(
    conn: sqlite3.Connection, run_limit: int
) -> list[sqlite3.Row]:
    """All recommendations from the most recent `run_limit` runs, newest first.

    One query (no N+1 across runs); the dashboard groups/dedupes by title in
    Python. Bounded by the same recent-runs window the dashboard already shows,
    so a recurring item surfaces with every date it was raised within that
    window.
    """
    return conn.execute(
        "SELECT run_date, title, severity, status, detail, issue_url, "
        "       created_ts, resolved_ts "
        "FROM consolidation_recommendations "
        "WHERE run_date IN ("
        "    SELECT run_date FROM consolidation_runs "
        "    ORDER BY started_ts DESC LIMIT ?) "
        "ORDER BY run_date DESC, created_ts DESC",
        (run_limit,),
    ).fetchall()
