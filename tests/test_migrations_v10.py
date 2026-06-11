"""v10 migration adds consolidation_runs.memories_verified."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from toolengrams import db


def test_fresh_db_has_memories_verified(tmp_path: Path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(consolidation_runs)").fetchall()}
    assert "memories_verified" in cols
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION
    conn.close()


def test_v9_db_upgrades_to_v10_default_zero(tmp_path: Path):
    """Existing rows get memories_verified=0 (default for NOT NULL column)."""
    path = tmp_path / "v9.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(db.SCHEMA_PATH.read_text())
    # Downgrade the v15 bits the live snapshot now carries: old DBs being
    # simulated here HAD the resume-era columns and LACKED origin_session_id.
    raw.executescript("""
        ALTER TABLE memories DROP COLUMN origin_session_id;
        ALTER TABLE watcher_state ADD COLUMN watcher_session_id TEXT;
        ALTER TABLE watcher_state ADD COLUMN watcher_pid INTEGER;
    """)
    # Strip everything added after v9 (v10 memories_verified, the v11
    # watcher_state columns, and the v12 noise_count + watcher_state re-key),
    # since the current schema.sql already carries them, so the forward
    # migration chain re-applies each cleanly.
    raw.executescript("""
        ALTER TABLE memories DROP COLUMN noise_count;
        DROP TABLE watcher_state;
        CREATE TABLE watcher_state (
            work_session_id    TEXT PRIMARY KEY,
            watcher_session_id TEXT,
            watcher_pid        INTEGER,
            transcript_path    TEXT,
            last_line_read     INTEGER NOT NULL DEFAULT 0,
            last_checked_ts    INTEGER NOT NULL,
            cwd                TEXT,
            created_ts         INTEGER NOT NULL
        );
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
            (run_date, started_ts, sessions_scanned)
            VALUES ('2026-05-10', 1000, 5);
    """)
    raw.execute("PRAGMA user_version = 9")
    raw.commit()
    raw.close()

    # Pre-migration: column doesn't exist.
    raw = sqlite3.connect(str(path))
    cols_before = {r[1] for r in raw.execute("PRAGMA table_info(consolidation_runs)").fetchall()}
    assert "memories_verified" not in cols_before
    raw.close()

    # Migrate.
    conn = db.connect(path)
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(consolidation_runs)").fetchall()}
    assert "memories_verified" in cols_after

    # Backfilled value on existing row is 0.
    row = conn.execute(
        "SELECT memories_verified FROM consolidation_runs WHERE run_date = '2026-05-10'"
    ).fetchone()
    assert row["memories_verified"] == 0
    conn.close()
