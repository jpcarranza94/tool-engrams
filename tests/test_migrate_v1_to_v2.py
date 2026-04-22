"""Unit tests for `engram migrate-v1-to-v2`."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from toolengrams.cli import migrate_v1_to_v2


def _build_v5_db(path):
    """Build a v1-era (user_version=5) DB by hand.

    We can't use db.connect() for this because the current schema.sql is
    already v7 shape. Write the exact v5 DDL instead.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE memories (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            description      TEXT,
            body             TEXT NOT NULL,
            type             TEXT NOT NULL CHECK (type IN ('feedback','reference')),
            scope            TEXT NOT NULL CHECK (scope IN ('global','project')) DEFAULT 'project',
            project_slug     TEXT,
            created_ts       INTEGER NOT NULL,
            last_surfaced_ts INTEGER NOT NULL DEFAULT 0,
            surface_count    INTEGER NOT NULL DEFAULT 0,
            useful_count     INTEGER NOT NULL DEFAULT 0,
            pinned           INTEGER NOT NULL DEFAULT 0,
            archived_ts      INTEGER
        );
        CREATE TABLE triggers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id       INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            kind            TEXT NOT NULL,
            tool_name       TEXT,
            head_joined     TEXT,
            head_length     INTEGER,
            path_pattern    TEXT,
            error_substring TEXT,
            keyword         TEXT
        );
        CREATE TABLE session_surfaces (
            session_id TEXT NOT NULL,
            memory_id INTEGER NOT NULL REFERENCES memories(id),
            surfaced_ts INTEGER NOT NULL,
            hook TEXT NOT NULL,
            tool_use_id TEXT,
            turn_at_surface INTEGER,
            PRIMARY KEY (session_id, memory_id, surfaced_ts)
        );
        CREATE TABLE memory_associations (
            memory_a_id INTEGER NOT NULL,
            memory_b_id INTEGER NOT NULL,
            strength REAL NOT NULL DEFAULT 0.0,
            co_fire_count INTEGER NOT NULL DEFAULT 0,
            last_co_fire_ts INTEGER NOT NULL DEFAULT 0,
            created_ts INTEGER NOT NULL,
            PRIMARY KEY (memory_a_id, memory_b_id),
            CHECK (memory_a_id < memory_b_id)
        );
        CREATE TABLE session_turns (
            session_id TEXT PRIMARY KEY,
            turn_count INTEGER NOT NULL DEFAULT 0,
            updated_ts INTEGER NOT NULL
        );
        CREATE TABLE consolidation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL UNIQUE,
            started_ts INTEGER NOT NULL,
            completed_ts INTEGER,
            sessions_scanned INTEGER NOT NULL DEFAULT 0,
            episodes_evaluated INTEGER NOT NULL DEFAULT 0,
            memories_strengthened INTEGER NOT NULL DEFAULT 0,
            memories_weakened INTEGER NOT NULL DEFAULT 0,
            memories_archived INTEGER NOT NULL DEFAULT 0,
            memories_discovered INTEGER NOT NULL DEFAULT 0,
            quality_score REAL,
            surfaces_helpful INTEGER NOT NULL DEFAULT 0,
            surfaces_noise INTEGER NOT NULL DEFAULT 0,
            report TEXT
        );
        CREATE TABLE watcher_state (
            work_session_id TEXT PRIMARY KEY,
            watcher_session_id TEXT,
            watcher_pid INTEGER,
            transcript_path TEXT,
            last_line_read INTEGER NOT NULL DEFAULT 0,
            last_checked_ts INTEGER NOT NULL,
            cwd TEXT,
            created_ts INTEGER NOT NULL
        );
    """)
    now_ts = int(time.time())
    # Two memories: one feedback, one reference.
    conn.execute(
        "INSERT INTO memories (name, body, type, scope, project_slug, created_ts) "
        "VALUES ('no-force-push', 'Never force push.', 'feedback', 'global', NULL, ?)",
        (now_ts,),
    )
    conn.execute(
        "INSERT INTO memories (name, body, type, scope, project_slug, created_ts) "
        "VALUES ('psql readonly', 'Replica is read-only.', 'reference', 'global', NULL, ?)",
        (now_ts,),
    )
    # v1-style tool_head triggers for both.
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, tool_name, head_joined, head_length) "
        "VALUES (1, 'tool_head', 'Bash', 'git push --force', 3)",
    )
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, tool_name, head_joined, head_length) "
        "VALUES (2, 'tool_head', 'Bash', 'psql -h', 2)",
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()


def test_migrate_runs_cleanly_on_v5_db(tmp_path, capsys):
    path = tmp_path / "v1.sqlite"
    _build_v5_db(path)

    rc = migrate_v1_to_v2.main(["--db", str(path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "migrated"
    assert payload["from_version"] == 5
    assert payload["to_version"] == 7
    # memories preserved (2 before, 2 after).
    assert payload["memories_before"] == 2
    assert payload["memories_after"] == 2
    # Values remapped: feedback→block (1), reference→hint (1).
    assert payload["kind_distribution_after"] == {"block": 1, "hint": 1}


def test_migrated_db_actually_usable(tmp_path):
    path = tmp_path / "v1.sqlite"
    _build_v5_db(path)
    migrate_v1_to_v2.main(["--db", str(path)])

    # Open and verify via normal schema.
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 7

    rows = conn.execute("SELECT kind, name FROM memories ORDER BY id").fetchall()
    assert rows[0]["kind"] == "block"
    assert rows[0]["name"] == "no-force-push"
    assert rows[1]["kind"] == "hint"
    assert rows[1]["name"] == "psql readonly"

    # memory_associations is gone.
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_associations'")
    assert cur.fetchone() is None

    # triggers table has the new shape.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(triggers)").fetchall()}
    assert "first_token" in cols
    assert "tokens_json" in cols
    assert "head_joined" not in cols
    conn.close()


def test_already_v2_db_is_noop(tmp_path, capsys):
    from toolengrams import db as engram_db

    path = tmp_path / "already-v2.sqlite"
    # Let db.connect create it at current version.
    conn = engram_db.connect(path)
    conn.close()

    rc = migrate_v1_to_v2.main(["--db", str(path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "noop"
    assert payload["reason"] == "already_v2"


def test_missing_db_reports_error(tmp_path, capsys):
    rc = migrate_v1_to_v2.main(["--db", str(tmp_path / "nope.sqlite")])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "db_not_found"


def test_dry_run_does_not_modify(tmp_path, capsys):
    path = tmp_path / "v1.sqlite"
    _build_v5_db(path)
    rc = migrate_v1_to_v2.main(["--db", str(path), "--dry-run"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "dry_run"
    assert payload["plan"]["from_version"] == 5
    assert payload["plan"]["to_version"] == 7
    assert payload["plan"]["value_map"]["memories.type=feedback → memories.kind=block"] == 1
    assert payload["plan"]["value_map"]["memories.type=reference → memories.kind=hint"] == 1

    # DB version is still 5.
    conn = sqlite3.connect(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    conn.close()


def test_unsupported_version_refused(tmp_path, capsys):
    path = tmp_path / "ancient.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE triggers (id INTEGER PRIMARY KEY)")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    rc = migrate_v1_to_v2.main(["--db", str(path)])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "refused"
    assert payload["reason"] == "unsupported_version"
