"""The persistence seam for the watcher run log (`watcher_runs` +
`watcher_run_events`).

Every SQL statement against those two tables lives here. `engram monitor` reads
them back into the live dashboard; `run_watcher_session` and the
`engram remember` / `engram judge` CLI calls write them.

Lifecycle of one run:
  start_run()   → a `running` row, committed before `claude -p` spawns so the
                  child CLI calls can reference it via $ENGRAM_RUN_ID.
  record_event()→ one row per memory the run created/judged (written by the CLI
                  child, tied back by run id).
  finish_run()  → finalize to `ok` / `error` with end time + window stats.
  reap_stale()  → a *new* tick for the same (session, role) marks any prior
                  still-`running` row `crashed` (the lock guarantees the old
                  run is gone). This is liveness without a heartbeat.

Convention (matching memory_store / session_state): every function takes an open
`conn` first; reads return raw `sqlite3.Row`s for the dashboard to format.
"""

from __future__ import annotations

import os
import sqlite3
import time


# ---------- writes (watcher path) ----------


def start_run(
    conn: sqlite3.Connection,
    *,
    work_session_id: str,
    role: str,
    pid: int,
    started_ts: int,
    model: str | None,
    flush: bool,
    cursor_from: int,
    cwd: str | None,
) -> int:
    """Insert a `running` run row, return its id. The caller commits (autocommit
    `db.session`) before spawning claude so the child can reference the id."""
    cur = conn.execute(
        "INSERT INTO watcher_runs "
        "(work_session_id, role, status, pid, started_ts, model, flush, "
        " cursor_from, cwd) "
        "VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)",
        (work_session_id, role, pid, started_ts, model, 1 if flush else 0,
         cursor_from, cwd),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    ended_ts: int,
    cursor_to: int | None = None,
    delta_chars: int | None = None,
    error: str | None = None,
) -> None:
    """Finalize a run row to `ok` / `error` with end time + window stats."""
    conn.execute(
        "UPDATE watcher_runs SET status = ?, ended_ts = ?, cursor_to = ?, "
        "delta_chars = ?, error = ? WHERE id = ?",
        (status, ended_ts, cursor_to, delta_chars, error, run_id),
    )


def reap_stale(conn: sqlite3.Connection, work_session_id: str, role: str,
               now_ts: int) -> int:
    """Mark any still-`running` row for this (session, role) as `crashed`. Called
    by a new tick after it takes the lock — a fresh run proves the old one died
    before finalizing. Returns the number of rows reaped."""
    cur = conn.execute(
        "UPDATE watcher_runs SET status = 'crashed', ended_ts = ? "
        "WHERE work_session_id = ? AND role = ? AND status = 'running'",
        (now_ts, work_session_id, role),
    )
    return cur.rowcount or 0


def record_event(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    ts: int,
    kind: str,
    memory_id: int | None,
    memory_name: str | None,
    outcome: str | None = None,
) -> None:
    """Record one memory a run created (`kind='created'`) or judged
    (`kind='judged'`, with `outcome`). Written by the engram CLI child."""
    conn.execute(
        "INSERT INTO watcher_run_events "
        "(run_id, ts, kind, memory_id, memory_name, outcome) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, ts, kind, memory_id, memory_name, outcome),
    )


def record_cli_event(conn: sqlite3.Connection, *, kind: str,
                     memory_id: int | None, memory_name: str | None,
                     outcome: str | None = None) -> None:
    """Record a created/judged event IF this CLI call is running inside a watcher
    session (`$ENGRAM_RUN_ID` set). No-op for manual or consolidation `engram`
    calls. Best-effort — never raises into the caller (it's telemetry)."""
    raw = os.environ.get("ENGRAM_RUN_ID")
    if not raw:
        return
    try:
        run_id = int(raw)
        record_event(conn, run_id=run_id, ts=int(time.time()), kind=kind,
                     memory_id=memory_id, memory_name=memory_name, outcome=outcome)
    except Exception:
        pass


def prune_runs_before(conn: sqlite3.Connection, cutoff_ts: int) -> int:
    """Delete runs started before `cutoff_ts` (and their events — explicitly, not
    relying on the FK cascade, since foreign_keys may be off). Returns runs
    deleted. Nightly-consolidation housekeeping."""
    conn.execute(
        "DELETE FROM watcher_run_events WHERE run_id IN "
        "(SELECT id FROM watcher_runs WHERE started_ts < ?)",
        (cutoff_ts,),
    )
    cur = conn.execute("DELETE FROM watcher_runs WHERE started_ts < ?", (cutoff_ts,))
    return cur.rowcount or 0


# ---------- reads (dashboard) ----------


def active_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All `running` rows, newest first. The dashboard decides live-vs-stale from
    `pid` liveness + the timeout window — this just returns the candidates."""
    return conn.execute(
        "SELECT * FROM watcher_runs WHERE status = 'running' "
        "ORDER BY started_ts DESC"
    ).fetchall()


def recent_runs(conn: sqlite3.Connection, since_ts: int,
                limit: int = 100) -> list[sqlite3.Row]:
    """Runs started since `since_ts`, newest first, each with its created/judged
    event counts — the 24h history pane."""
    return conn.execute(
        "SELECT r.*, "
        "  (SELECT COUNT(*) FROM watcher_run_events e "
        "     WHERE e.run_id = r.id AND e.kind = 'created') AS n_created, "
        "  (SELECT COUNT(*) FROM watcher_run_events e "
        "     WHERE e.run_id = r.id AND e.kind = 'judged') AS n_judged "
        "FROM watcher_runs r WHERE r.started_ts >= ? "
        "ORDER BY r.started_ts DESC LIMIT ?",
        (since_ts, limit),
    ).fetchall()


def recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Newest decision-stream events with their run's role + session — created
    (formation) vs judged (eval), newest first."""
    return conn.execute(
        "SELECT e.ts, e.kind, e.memory_id, e.memory_name, e.outcome, "
        "       r.role, r.work_session_id "
        "FROM watcher_run_events e JOIN watcher_runs r ON r.id = e.run_id "
        "ORDER BY e.ts DESC LIMIT ?",
        (limit,),
    ).fetchall()


def counts_since(conn: sqlite3.Connection, since_ts: int) -> dict:
    """Aggregate run/event counts since `since_ts` for the non-TTY JSON snapshot
    and the dashboard header."""
    runs = conn.execute(
        "SELECT status, COUNT(*) AS n FROM watcher_runs "
        "WHERE started_ts >= ? GROUP BY status",
        (since_ts,),
    ).fetchall()
    events = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM watcher_run_events "
        "WHERE ts >= ? GROUP BY kind",
        (since_ts,),
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in runs}
    by_kind = {r["kind"]: r["n"] for r in events}
    return {
        "runs_by_status": by_status,
        "created": by_kind.get("created", 0),
        "judged": by_kind.get("judged", 0),
    }
