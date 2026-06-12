"""Tests for db.py — schema + migration runner + connection handling."""

from __future__ import annotations

from toolengrams import db, paths


def test_fresh_db_gets_all_tables(tmp_path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "memories" in tables
    assert "triggers" in tables
    assert "session_surfaces" in tables
    assert "session_turns" in tables
    assert "consolidation_runs" in tables
    assert "watcher_state" in tables
    # The schema has no memory_associations table.
    assert "memory_associations" not in tables

    # memories uses .kind (not .type); triggers has first_token + tokens_json.
    mem_cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "kind" in mem_cols
    assert "type" not in mem_cols
    trigger_cols = {r[1] for r in conn.execute("PRAGMA table_info(triggers)").fetchall()}
    assert "first_token" in trigger_cols
    assert "tokens_json" in trigger_cols
    assert "head_joined" not in trigger_cols

    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == db.SCHEMA_VERSION
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
    conn = db.connect(path)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == db.SCHEMA_VERSION
    conn.close()


def test_env_var_override(tmp_path, monkeypatch):
    override = tmp_path / "custom.sqlite"
    monkeypatch.setenv("ENGRAM_DB", str(override))
    assert db.db_path() == override


def test_default_db_path(monkeypatch):
    monkeypatch.delenv("ENGRAM_DB", raising=False)
    assert db.db_path() == paths.engram_home() / "db.sqlite"
