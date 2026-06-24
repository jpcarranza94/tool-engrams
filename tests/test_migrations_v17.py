"""v17 migration adds triggers.access_mode + backfills path_glob rows to 'write'."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from toolengrams import db


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_access_mode_column(tmp_path: Path):
    conn = db.connect(tmp_path / "fresh.sqlite")
    assert "access_mode" in _cols(conn, "triggers")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_v16_db_backfills_path_triggers_to_write(tmp_path: Path):
    """A simulated v16 DB (no access_mode column) upgrades in place: existing
    path_glob triggers backfill to 'write', token_subseq rows stay NULL, and
    the memory's reinforcement counters are untouched."""
    path = tmp_path / "v16.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(db.SCHEMA_PATH.read_text())
    raw.executescript("ALTER TABLE triggers DROP COLUMN access_mode;")
    raw.execute(
        "INSERT INTO memories (id, name, description, body, kind, scope, "
        " project_slug, created_ts, useful_count, noise_count, surface_count) "
        "VALUES (1, 'm', '', 'b', 'hint', 'global', NULL, 1, 7, 2, 9)")
    raw.execute("INSERT INTO triggers (memory_id, kind, path_pattern) "
                "VALUES (1, 'path_glob', '**/*.py')")
    raw.execute("INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
                "VALUES (1, 'token_subseq', 'git', '[\"git\"]')")
    raw.execute("PRAGMA user_version = 16")
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 17
    modes = {r["kind"]: r["access_mode"] for r in
             conn.execute("SELECT kind, access_mode FROM triggers").fetchall()}
    assert modes["path_glob"] == "write"
    assert modes["token_subseq"] is None
    m = conn.execute(
        "SELECT useful_count, noise_count, surface_count FROM memories WHERE id = 1"
    ).fetchone()
    assert (m["useful_count"], m["noise_count"], m["surface_count"]) == (7, 2, 9)
    conn.close()
