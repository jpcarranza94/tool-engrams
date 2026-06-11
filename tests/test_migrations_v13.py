"""v13 migration: the live-monitor run log (watcher_runs + watcher_run_events)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from toolengrams import db


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}


def test_fresh_db_has_run_tables(tmp_path: Path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
    tables = _tables(conn)
    assert "watcher_runs" in tables
    assert "watcher_run_events" in tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_v12_db_upgrades_to_get_run_tables(tmp_path: Path):
    path = tmp_path / "v12.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(db.SCHEMA_PATH.read_text())
    # Downgrade the v15 bits the live snapshot now carries: old DBs being
    # simulated here HAD the resume-era columns and LACKED origin_session_id.
    raw.executescript("""
        ALTER TABLE memories DROP COLUMN origin_session_id;
        ALTER TABLE watcher_state ADD COLUMN watcher_session_id TEXT;
        ALTER TABLE watcher_state ADD COLUMN watcher_pid INTEGER;
    """)
    # Simulate a real v12 DB: drop the v13 tables, force user_version=12.
    raw.executescript("""
        DROP TABLE IF EXISTS watcher_run_events;
        DROP TABLE IF EXISTS watcher_runs;
    """)
    raw.execute("PRAGMA user_version = 12")
    raw.commit()
    raw.close()

    # Pre-migration: tables absent.
    raw = sqlite3.connect(str(path))
    assert "watcher_runs" not in _tables(raw)
    raw.close()

    # Migrate.
    conn = db.connect(path)
    tables = _tables(conn)
    assert "watcher_runs" in tables
    assert "watcher_run_events" in tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
