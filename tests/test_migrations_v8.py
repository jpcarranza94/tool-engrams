"""v8 migration adds memories.last_verified_ts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from toolengrams import db


def test_fresh_db_has_last_verified_ts(tmp_path: Path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "last_verified_ts" in cols
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION
    conn.close()


def test_v7_db_upgrades_to_v8(tmp_path: Path):
    # Build a v7 DB manually by applying the schema then forcing user_version=7.
    path = tmp_path / "v7.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(db.SCHEMA_PATH.read_text())
    # Strip the new column to simulate a real v7 DB without it.
    raw.executescript("""
        ALTER TABLE memories RENAME TO memories_tmp;
        CREATE TABLE memories (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            description      TEXT,
            body             TEXT NOT NULL,
            kind             TEXT NOT NULL CHECK (kind IN ('block','hint')),
            scope            TEXT NOT NULL CHECK (scope IN ('global','project')) DEFAULT 'project',
            project_slug     TEXT,
            created_ts       INTEGER NOT NULL,
            last_surfaced_ts INTEGER NOT NULL DEFAULT 0,
            surface_count    INTEGER NOT NULL DEFAULT 0,
            useful_count     INTEGER NOT NULL DEFAULT 0,
            pinned           INTEGER NOT NULL DEFAULT 0,
            archived_ts      INTEGER
        );
        INSERT INTO memories
            SELECT id, name, description, body, kind, scope, project_slug,
                   created_ts, last_surfaced_ts, surface_count, useful_count,
                   pinned, archived_ts
            FROM memories_tmp;
        DROP TABLE memories_tmp;
    """)
    raw.execute("PRAGMA user_version = 7")
    raw.commit()
    raw.close()

    # Sanity: column doesn't exist yet on this simulated v7 DB.
    raw = sqlite3.connect(str(path))
    cols_before = {r[1] for r in raw.execute("PRAGMA table_info(memories)").fetchall()}
    assert "last_verified_ts" not in cols_before
    raw.close()

    # Open via db.connect → migration runs.
    conn = db.connect(path)
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "last_verified_ts" in cols_after
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION
    conn.close()


def test_new_column_defaults_to_null(tmp_path: Path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
    import time

    now_ts = int(time.time())
    conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES ('m', '', 'b', 'hint', 'global', NULL, ?)",
        (now_ts,),
    )
    row = conn.execute(
        "SELECT last_verified_ts FROM memories WHERE name = 'm'"
    ).fetchone()
    assert row["last_verified_ts"] is None
    conn.close()
