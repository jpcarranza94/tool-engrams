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
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == db.SCHEMA_VERSION
    conn.close()


def test_v1_to_v2_migration(tmp_path):
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA_PATH.read_text())
    conn.execute("PRAGMA user_version = 1")
    conn.close()

    # Reopen via db.connect — should migrate to v2.
    conn = db.connect(path)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 2
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "memory_associations" in tables
    assert "consolidation_runs" in tables
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
