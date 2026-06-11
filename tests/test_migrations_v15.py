"""v15 migration: origin_session_id on memories, 'quarantined' run-event kind +
detail column, watcher_state loses the resume-era columns (rebuild pattern)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from toolengrams import db


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_v15_shape(tmp_path: Path):
    conn = db.connect(tmp_path / "fresh.sqlite")
    assert "origin_session_id" in _cols(conn, "memories")
    assert "detail" in _cols(conn, "watcher_run_events")
    state_cols = _cols(conn, "watcher_state")
    assert "watcher_session_id" not in state_cols
    assert "watcher_pid" not in state_cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def _make_v14_db(path: Path) -> None:
    """Simulate a v14 DB: current snapshot minus the v15 changes, with data."""
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        PRAGMA foreign_keys = OFF;
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, description TEXT, body TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('block','hint')),
            scope TEXT NOT NULL CHECK (scope IN ('global','project')) DEFAULT 'project',
            project_slug TEXT, created_ts INTEGER NOT NULL,
            last_surfaced_ts INTEGER NOT NULL DEFAULT 0,
            surface_count INTEGER NOT NULL DEFAULT 0,
            useful_count INTEGER NOT NULL DEFAULT 0,
            noise_count INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0,
            archived_ts INTEGER, last_verified_ts INTEGER DEFAULT NULL
        );
        CREATE TABLE triggers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('token_subseq','path_glob')),
            first_token TEXT, tokens_json TEXT, path_pattern TEXT
        );
        CREATE TABLE watcher_state (
            work_session_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'formation' CHECK (role IN ('formation','eval')),
            watcher_session_id TEXT, watcher_pid INTEGER, transcript_path TEXT,
            last_line_read INTEGER NOT NULL DEFAULT 0,
            last_checked_ts INTEGER NOT NULL, cwd TEXT, created_ts INTEGER NOT NULL,
            armed INTEGER NOT NULL DEFAULT 0,
            last_tick_ts INTEGER NOT NULL DEFAULT 0,
            fail_streak INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (work_session_id, role)
        );
        CREATE TABLE watcher_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('formation','eval')),
            status TEXT NOT NULL CHECK (status IN ('running','ok','error','crashed'))
              DEFAULT 'running',
            pid INTEGER, started_ts INTEGER NOT NULL, ended_ts INTEGER, model TEXT,
            flush INTEGER NOT NULL DEFAULT 0, cursor_from INTEGER, cursor_to INTEGER,
            delta_chars INTEGER, cwd TEXT, error TEXT, cost_usd REAL,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_creation_tokens INTEGER
        );
        CREATE TABLE watcher_run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES watcher_runs(id) ON DELETE CASCADE,
            ts INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('created','judged')),
            memory_id INTEGER, memory_name TEXT,
            outcome TEXT CHECK (outcome IN ('helpful','unused','noise') OR outcome IS NULL)
        );
    """)
    now = int(time.time())
    raw.execute("INSERT INTO memories (name, body, kind, created_ts) "
                "VALUES ('m1', 'b1', 'hint', ?)", (now,))
    raw.execute("INSERT INTO watcher_state (work_session_id, role, watcher_session_id, "
                "last_line_read, last_checked_ts, created_ts, last_tick_ts) "
                "VALUES ('s1', 'formation', 'dead-resume-id', 42, ?, ?, ?)",
                (now, now, now))
    raw.execute("INSERT INTO watcher_runs (work_session_id, role, status, started_ts) "
                "VALUES ('s1', 'formation', 'ok', ?)", (now,))
    raw.execute("INSERT INTO watcher_run_events (run_id, ts, kind, memory_id, memory_name) "
                "VALUES (1, ?, 'created', 1, 'm1')", (now,))
    raw.execute("PRAGMA user_version = 14")
    raw.commit()
    raw.close()


def test_v14_db_upgrades_preserving_rows(tmp_path: Path):
    path = tmp_path / "v14.sqlite"
    _make_v14_db(path)

    conn = db.connect(path)  # runs migrations
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION

    # memories gained the column, existing row reads as NULL origin.
    row = conn.execute("SELECT origin_session_id FROM memories WHERE id = 1").fetchone()
    assert row["origin_session_id"] is None

    # watcher_state row survives, cursor intact, resume columns gone.
    st = conn.execute("SELECT * FROM watcher_state WHERE work_session_id = 's1'").fetchone()
    assert st["last_line_read"] == 42
    assert "watcher_session_id" not in st.keys()
    assert "watcher_pid" not in st.keys()

    # run events survive (id + join intact) and the new kind is accepted.
    ev = conn.execute("SELECT * FROM watcher_run_events WHERE id = 1").fetchone()
    assert ev["kind"] == "created" and ev["memory_id"] == 1 and ev["detail"] is None
    conn.execute(
        "INSERT INTO watcher_run_events (run_id, ts, kind, memory_id, memory_name, detail) "
        "VALUES (1, ?, 'quarantined', 1, 'm1', 'harmful: broke the build')",
        (int(time.time()),),
    )
    # FK text must name the real table after the rename (regression guard).
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'watcher_run_events'"
    ).fetchone()["sql"]
    assert "REFERENCES watcher_runs" in ddl and "_v15" not in ddl
    conn.close()
