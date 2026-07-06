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

from .. import db

# Recent-run window for the standing OPEN recommendation backlog (WS5.1/WS5.2):
# the nightly agent (agent.py) and the manual `engram recommend` CLI both read
# `open_recommendations` over this many recent runs. Owned here beside the query
# so the window can't drift between callers. Distinct from the dashboard's
# cross-run window (`recommendations_across_runs`, run_limit=10) — that view
# shows every date an item was raised; this one dedupes to the standing backlog.
OPEN_BACKLOG_RUN_WINDOW = 7

# Unbounded run window for the human `engram recommend --list` path: a still-open
# rec raised beyond OPEN_BACKLOG_RUN_WINDOW must stay discoverable to a maintainer
# (the agent only re-emits titles it was shown, so an aged-out open title can
# never re-enter the agent's window on its own). SQLite treats `LIMIT -1` as no
# upper bound, so this widens open_recommendations to every recorded run.
ALL_RUNS = -1

# The recent-run-dates subselect shared by the two cross-run recommendation
# reads (across-runs display + open backlog); interpolated into each query so
# the bounding window can't drift between them.
_RECENT_RUN_DATES = (
    "SELECT run_date FROM consolidation_runs ORDER BY started_ts DESC LIMIT ?"
)


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

    Delete + insert run in one transaction so a concurrent reader (the dashboard)
    never observes the day with its recommendations momentarily cleared — unlike
    `record_run`'s single-statement upsert, this is two statements.
    """
    with db.transaction(conn):
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
        "WHERE run_date IN (" + _RECENT_RUN_DATES + ") "
        "ORDER BY run_date DESC, created_ts DESC",
        (run_limit,),
    ).fetchall()


def open_recommendations(
    conn: sqlite3.Connection, run_limit: int
) -> list[sqlite3.Row]:
    """The standing OPEN backlog, one row per title, for the nightly agent (WS5.1).

    The agent never saw its own prior advisories, so it re-raised recurring issues
    as fresh `open` rows every night (24 open / 4 ever done). Injecting this
    backlog lets Task 6 re-affirm-or-resolve instead of duplicate.

    Semantics that matter:
    - **Newest-status per title.** A title may have a row per run_date; we keep
      only the *newest* row (created_ts DESC, id DESC) and return it if that row
      is still `open`. So a prior run's `done` re-emission OR a manual
      `engram recommend --close` (which stamps the existing rows `done`) drops the
      title out of the backlog — the agent is never prompted to re-raise it, which
      is what keeps a manual close STICKY (see resolve_recommendation).
    - **Critical excluded.** `severity='critical'` is the code-bug / data-safety
      tier: the agent cannot verify a maintainer's code fix actually shipped, so
      showing it in the backlog would make the agent wrongly re-open it forever.
      Those are closed ONLY via `engram recommend --close` (WS5.2).

    Bounded by the same recent-runs window as the dashboard. Titles are compared
    casefolded (LOWER) to match the cross-run dedup key.
    """
    return conn.execute(
        "SELECT title, severity, status, detail, issue_url, run_date, "
        "       created_ts, resolved_ts FROM ("
        "  SELECT title, severity, status, detail, issue_url, run_date, "
        "         created_ts, resolved_ts, "
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY LOWER(title) "
        "           ORDER BY created_ts DESC, id DESC) AS rn "
        "  FROM consolidation_recommendations "
        "  WHERE run_date IN (" + _RECENT_RUN_DATES + ")) "
        "WHERE rn = 1 AND status = 'open' AND severity != 'critical' "
        "ORDER BY created_ts DESC",
        (run_limit,),
    ).fetchall()


def resolve_recommendation(
    conn: sqlite3.Connection, title: str, *, now_ts: int
) -> int:
    """Mark every stored recommendation with this (casefolded) title `done` —
    the manual-close path for `engram recommend --close` (WS5.2).

    This is the first status write outside `insert_recommendations`. It stamps
    `status='done'` + `resolved_ts` on ALL matching rows across every run_date, so
    the title's newest row is `done` and `open_recommendations` immediately stops
    surfacing it to the agent. That exclusion is what makes a manual close STICKY:
    the agent only re-emits titles it was shown in the backlog, so a closed title
    is never reflexively re-raised (a genuine fresh re-detection is still allowed —
    that is a real recurrence, not a clobber). Returns rows updated (0 == no such
    title). Idempotent: re-closing an already-done title is a harmless no-op
    re-stamp.
    """
    cur = conn.execute(
        "UPDATE consolidation_recommendations "
        "SET status = 'done', resolved_ts = ? "
        "WHERE LOWER(title) = LOWER(?)",
        (now_ts, title),
    )
    return cur.rowcount or 0
