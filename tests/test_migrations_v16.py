"""v16 migration adds watcher_state.target + watcher_runs.engine."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from toolengrams import db


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_harness_columns(tmp_path: Path):
    conn = db.connect(tmp_path / "fresh.sqlite")
    assert "target" in _cols(conn, "watcher_state")
    assert "engine" in _cols(conn, "watcher_runs")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_v15_db_upgrades_with_claude_code_default(tmp_path: Path):
    """A simulated v15 DB (no target/engine columns) upgrades in place; the
    existing watcher_state row reads back target='claude-code'."""
    path = tmp_path / "v15.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(db.SCHEMA_PATH.read_text())
    raw.executescript("""
        ALTER TABLE watcher_state DROP COLUMN target;
        ALTER TABLE watcher_runs DROP COLUMN engine;
    """)
    raw.execute(
        "INSERT INTO watcher_state (work_session_id, role, transcript_path, "
        " last_checked_ts, created_ts) VALUES ('s1', 'formation', '/t', 1, 1)")
    raw.execute("PRAGMA user_version = 15")
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 16
    row = conn.execute(
        "SELECT target FROM watcher_state WHERE work_session_id = 's1'"
    ).fetchone()
    assert row["target"] == "claude-code"
    conn.close()
