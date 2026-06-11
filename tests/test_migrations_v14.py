"""v14 migration adds cost/token columns to watcher_runs (rebuild pattern)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from toolengrams import db

_COST_COLS = {"cost_usd", "input_tokens", "output_tokens",
              "cache_read_tokens", "cache_creation_tokens"}


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_cost_columns(tmp_path: Path):
    conn = db.connect(tmp_path / "fresh.sqlite")
    assert _COST_COLS <= _cols(conn, "watcher_runs")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_v13_db_upgrades_preserving_rows_and_fk(tmp_path: Path):
    """A simulated v13 DB (watcher_runs without cost columns) upgrades via the
    rebuild: rows and ids survive, events still join, FK text still names
    watcher_runs (not the tmp table)."""
    path = tmp_path / "v13.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(db.SCHEMA_PATH.read_text())
    # Downgrade the v15 bits the live snapshot now carries: old DBs being
    # simulated here HAD the resume-era columns and LACKED origin_session_id.
    raw.executescript("""
        ALTER TABLE memories DROP COLUMN origin_session_id;
        ALTER TABLE watcher_state ADD COLUMN watcher_session_id TEXT;
        ALTER TABLE watcher_state ADD COLUMN watcher_pid INTEGER;
    """)
    raw.executescript("""
        PRAGMA foreign_keys = OFF;
        PRAGMA legacy_alter_table = ON;
        ALTER TABLE watcher_runs RENAME TO wr_tmp;
        CREATE TABLE watcher_runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            work_session_id  TEXT NOT NULL,
            role             TEXT NOT NULL CHECK (role IN ('formation','eval')),
            status           TEXT NOT NULL CHECK (status IN ('running','ok','error','crashed'))
                               DEFAULT 'running',
            pid              INTEGER,
            started_ts       INTEGER NOT NULL,
            ended_ts         INTEGER,
            model            TEXT,
            flush            INTEGER NOT NULL DEFAULT 0,
            cursor_from      INTEGER,
            cursor_to        INTEGER,
            delta_chars      INTEGER,
            cwd              TEXT,
            error            TEXT
        );
        DROP TABLE wr_tmp;
        PRAGMA legacy_alter_table = OFF;
    """)
    raw.execute(
        "INSERT INTO watcher_runs (id, work_session_id, role, status, started_ts) "
        "VALUES (7, 's1', 'formation', 'ok', 123)")
    raw.execute(
        "INSERT INTO watcher_run_events (run_id, ts, kind, memory_id, memory_name) "
        "VALUES (7, 124, 'created', 1, 'm')")
    raw.execute("PRAGMA user_version = 13")
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert _COST_COLS <= _cols(conn, "watcher_runs")
    row = conn.execute("SELECT * FROM watcher_runs WHERE id = 7").fetchone()
    assert row["work_session_id"] == "s1" and row["cost_usd"] is None
    joined = conn.execute(
        "SELECT COUNT(*) FROM watcher_run_events e "
        "JOIN watcher_runs r ON r.id = e.run_id").fetchone()[0]
    assert joined == 1
    fk_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'watcher_run_events'"
    ).fetchone()[0]
    assert "watcher_runs" in fk_sql and "tmp" not in fk_sql
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_v14_double_apply_is_safe_and_restores_fk_default(tmp_path: Path):
    """The migration's idempotency claim, pinned: re-running v14 on an
    already-v14 DB (snapshot shape, or a crash after COMMIT but before the
    user_version bump) must leave a working table. And the script must end
    with foreign_keys OFF — the app never enables enforcement, so the
    migrating connection must not be the one connection that does."""
    path = tmp_path / "v14.sqlite"
    conn = db.connect(path)
    v14_sql = (db.MIGRATIONS_DIR / "v14.sql").read_text()
    conn.executescript(v14_sql)

    assert _COST_COLS <= _cols(conn, "watcher_runs")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    conn.execute(
        "INSERT INTO watcher_runs (work_session_id, role, status, started_ts) "
        "VALUES ('s', 'formation', 'ok', 1)")
    conn.close()
