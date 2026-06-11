"""v9 migration: session_surfaces.first_token + outcome.

Covers the deliberately-scoped backfill: pre_tool_use rows get a best-effort
first_token, post_tool_use_failure rows stay NULL (avoiding mis-credit on
the new same-first_token reinforcement path).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from toolengrams import db


def _build_v8_db(path: Path) -> None:
    """Apply the current schema then strip v9 columns and force user_version=8."""
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
        DROP INDEX IF EXISTS idx_session_surfaces_failure_token;
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
    raw.execute("PRAGMA user_version = 8")
    raw.commit()
    raw.close()


def test_backfill_populates_pre_tool_use_surfaces(tmp_path):
    path = tmp_path / "v8.sqlite"
    _build_v8_db(path)

    raw = sqlite3.connect(str(path))
    now_ts = int(time.time())
    raw.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES ('m', '', 'b', 'hint', 'global', NULL, ?)",
        (now_ts,),
    )
    mid = raw.execute("SELECT id FROM memories WHERE name = 'm'").fetchone()[0]
    raw.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', 'git', '[\"git\", \"push\"]')",
        (mid,),
    )
    raw.execute(
        "INSERT INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES ('sess-X', ?, 1000, 'pre_tool_use', 'tu-1', 1)",
        (mid,),
    )
    raw.commit()
    raw.close()

    # Apply the migration.
    conn = db.connect(path)
    row = conn.execute(
        "SELECT first_token FROM session_surfaces WHERE memory_id = ?", (mid,)
    ).fetchone()
    assert row["first_token"] == "git"
    conn.close()


def test_backfill_skips_post_tool_use_failure_surfaces(tmp_path):
    """Failure surfaces are deliberately left NULL — backfilling them with
    an arbitrary trigger's first_token could mis-credit on next retry."""
    path = tmp_path / "v8.sqlite"
    _build_v8_db(path)

    raw = sqlite3.connect(str(path))
    now_ts = int(time.time())
    raw.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES ('m', '', 'b', 'hint', 'global', NULL, ?)",
        (now_ts,),
    )
    mid = raw.execute("SELECT id FROM memories WHERE name = 'm'").fetchone()[0]
    raw.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', 'git', '[\"git\", \"push\"]')",
        (mid,),
    )
    raw.execute(
        "INSERT INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES ('sess-X', ?, 1000, 'post_tool_use_failure', 'tu-1', 1)",
        (mid,),
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)
    row = conn.execute(
        "SELECT first_token FROM session_surfaces WHERE memory_id = ?", (mid,)
    ).fetchone()
    assert row["first_token"] is None  # deliberately not backfilled
    conn.close()


def test_outcome_check_constraint_rejects_bogus_value(tmp_path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
    now_ts = int(time.time())
    conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES ('m', '', 'b', 'hint', 'global', NULL, ?)",
        (now_ts,),
    )
    mid = conn.execute("SELECT id FROM memories WHERE name = 'm'").fetchone()[0]

    try:
        conn.execute(
            "INSERT INTO session_surfaces "
            "(session_id, memory_id, surfaced_ts, hook, outcome) "
            "VALUES ('s', ?, 1, 'pre_tool_use', 'invalid-outcome')",
            (mid,),
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised
