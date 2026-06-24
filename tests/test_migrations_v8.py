"""v8 migration adds memories.last_verified_ts."""

from __future__ import annotations

import sqlite3
import time
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


def test_v7_db_upgrades_through_all_pending(tmp_path: Path):
    """A simulated v7 DB upgrades cleanly through every pending migration."""
    path = tmp_path / "v7.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(db.SCHEMA_PATH.read_text())
    # Downgrade bits the live snapshot now carries but these older DBs lacked:
    # the v15 resume-era columns + origin_session_id, and the v17 triggers
    # access_mode column (the forward migration chain re-applies each cleanly).
    raw.executescript("""
        ALTER TABLE memories DROP COLUMN origin_session_id;
        ALTER TABLE watcher_state ADD COLUMN watcher_session_id TEXT;
        ALTER TABLE watcher_state ADD COLUMN watcher_pid INTEGER;
        ALTER TABLE triggers DROP COLUMN access_mode;
    """)
    # Strip post-v7 columns/indices to simulate a real v7 DB.
    raw.executescript("""
        DROP INDEX IF EXISTS idx_session_surfaces_failure_token;
        ALTER TABLE watcher_state DROP COLUMN armed;
        ALTER TABLE watcher_state DROP COLUMN last_tick_ts;
        ALTER TABLE watcher_state DROP COLUMN fail_streak;
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

        ALTER TABLE session_surfaces RENAME TO session_surfaces_tmp;
        CREATE TABLE session_surfaces (
            session_id       TEXT NOT NULL,
            memory_id        INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            surfaced_ts      INTEGER NOT NULL,
            hook             TEXT NOT NULL,
            tool_use_id      TEXT,
            turn_at_surface  INTEGER,
            PRIMARY KEY (session_id, memory_id, surfaced_ts)
        );
        INSERT INTO session_surfaces
            SELECT session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface
            FROM session_surfaces_tmp;
        DROP TABLE session_surfaces_tmp;

        ALTER TABLE consolidation_runs RENAME TO consolidation_runs_tmp;
        CREATE TABLE consolidation_runs (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date               TEXT NOT NULL UNIQUE,
            started_ts             INTEGER NOT NULL,
            completed_ts           INTEGER,
            sessions_scanned       INTEGER NOT NULL DEFAULT 0,
            episodes_evaluated     INTEGER NOT NULL DEFAULT 0,
            memories_strengthened  INTEGER NOT NULL DEFAULT 0,
            memories_weakened      INTEGER NOT NULL DEFAULT 0,
            memories_archived      INTEGER NOT NULL DEFAULT 0,
            memories_discovered    INTEGER NOT NULL DEFAULT 0,
            quality_score          REAL,
            surfaces_helpful       INTEGER NOT NULL DEFAULT 0,
            surfaces_noise         INTEGER NOT NULL DEFAULT 0,
            report                 TEXT
        );
        INSERT INTO consolidation_runs
            SELECT id, run_date, started_ts, completed_ts, sessions_scanned,
                   episodes_evaluated, memories_strengthened, memories_weakened,
                   memories_archived, memories_discovered, quality_score,
                   surfaces_helpful, surfaces_noise, report
            FROM consolidation_runs_tmp;
        DROP TABLE consolidation_runs_tmp;
    """)
    raw.execute("PRAGMA user_version = 7")
    raw.commit()
    raw.close()

    raw = sqlite3.connect(str(path))
    cols_before = {r[1] for r in raw.execute("PRAGMA table_info(memories)").fetchall()}
    assert "last_verified_ts" not in cols_before
    surf_cols_before = {r[1] for r in raw.execute("PRAGMA table_info(session_surfaces)").fetchall()}
    assert "first_token" not in surf_cols_before
    assert "outcome" not in surf_cols_before
    raw.close()

    # Open via db.connect → migrations v8 + v9 run in sequence.
    conn = db.connect(path)
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "last_verified_ts" in cols_after
    surf_cols_after = {r[1] for r in conn.execute("PRAGMA table_info(session_surfaces)").fetchall()}
    assert "first_token" in surf_cols_after
    assert "outcome" in surf_cols_after
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION
    conn.close()


def test_new_column_defaults_to_null(tmp_path: Path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
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
