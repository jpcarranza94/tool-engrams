"""Tests for db.py — migration logic and connection handling."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from toolengrams import db


def test_fresh_db_gets_all_tables(tmp_path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "memories" in tables
    assert "triggers" in tables
    assert "session_surfaces" in tables
    assert "memory_associations" in tables
    assert "consolidation_runs" in tables
    assert "session_turns" in tables
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == db.SCHEMA_VERSION
    conn.close()


def test_v1_to_latest_migration(tmp_path):
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA_PATH.read_text())
    conn.execute("PRAGMA user_version = 1")
    conn.close()

    # Reopen via db.connect — should migrate all the way to current.
    conn = db.connect(path)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == db.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "memory_associations" in tables
    assert "consolidation_runs" in tables
    assert "session_turns" in tables
    conn.close()


def test_v2_to_v3_adds_turn_at_surface_column(tmp_path):
    """Existing v2 DBs should pick up the turn_at_surface column + session_turns table."""
    path = tmp_path / "v2.sqlite"
    # Build a v2-shape DB manually: schema + v2.sql, user_version=2.
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA_PATH.read_text())
    v2_sql = (db.MIGRATIONS_DIR / "v2.sql").read_text()
    conn.executescript(v2_sql)
    conn.execute("PRAGMA user_version = 2")
    conn.close()

    # Reopen — should migrate to latest.
    conn = db.connect(path)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == db.SCHEMA_VERSION

    # session_turns exists and is empty.
    row = conn.execute("SELECT COUNT(*) AS n FROM session_turns").fetchone()
    assert row["n"] == 0

    # session_surfaces has the new turn_at_surface column.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(session_surfaces)").fetchall()}
    assert "turn_at_surface" in cols

    conn.close()


def test_v3_index_on_turn_at_surface_exists(tmp_path):
    path = tmp_path / "idx.sqlite"
    conn = db.connect(path)
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_session_surfaces_turn" in indexes
    conn.close()


def test_already_at_current_version_is_noop(tmp_path):
    path = tmp_path / "current.sqlite"
    conn = db.connect(path)
    conn.close()
    # Reopen — should not error.
    conn = db.connect(path)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == db.SCHEMA_VERSION
    conn.close()


def test_env_var_override(tmp_path, monkeypatch):
    override = tmp_path / "custom.sqlite"
    monkeypatch.setenv("ENGRAM_DB", str(override))
    assert db.db_path() == override


def test_default_db_path():
    expected = Path.home() / ".claude" / "tool-engrams" / "db.sqlite"
    assert db.DEFAULT_DB_PATH == expected
