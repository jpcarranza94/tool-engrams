"""v12 migration: the scoring cutover.

Three changes, all in `migrations/v12.sql`:
  (1) memories.noise_count — a counter the evaluation watcher bumps on a
      'noise' verdict (the trigger over-matched).
  (2) useful_count reset to 0 — the legacy useful_count column carries no
      real signal, so it is wiped to a clean slate. surface_count is KEPT.
  (3) watcher_state re-keyed (work_session_id, role) — formation and eval
      each get a symmetric row per session. Existing rows → role='formation'.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from toolengrams import db


def _watcher_state_pk(conn: sqlite3.Connection) -> set[str]:
    """The set of column names that make up watcher_state's primary key."""
    return {
        r[1]
        for r in conn.execute("PRAGMA table_info(watcher_state)").fetchall()
        if r[5]  # pk ordinal > 0 → part of the primary key
    }


def _build_v11_db(path: Path) -> None:
    """Apply the current schema then strip the v12 changes to simulate a real
    v11 DB and force user_version=11."""
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
        ALTER TABLE memories DROP COLUMN noise_count;

        ALTER TABLE watcher_state RENAME TO watcher_state_tmp;
        CREATE TABLE watcher_state (
            work_session_id    TEXT PRIMARY KEY,
            watcher_session_id TEXT,
            watcher_pid        INTEGER,
            transcript_path    TEXT,
            last_line_read     INTEGER NOT NULL DEFAULT 0,
            last_checked_ts    INTEGER NOT NULL,
            cwd                TEXT,
            created_ts         INTEGER NOT NULL,
            armed              INTEGER NOT NULL DEFAULT 0,
            last_tick_ts       INTEGER NOT NULL DEFAULT 0,
            fail_streak        INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO watcher_state
            SELECT work_session_id, watcher_session_id, watcher_pid, transcript_path,
                   last_line_read, last_checked_ts, cwd, created_ts,
                   armed, last_tick_ts, fail_streak
            FROM watcher_state_tmp;
        DROP TABLE watcher_state_tmp;
    """)
    raw.execute("PRAGMA user_version = 11")
    raw.commit()
    raw.close()


def test_fresh_db_has_v12_shape(tmp_path: Path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)

    mem_cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert "noise_count" in mem_cols

    ws_cols = {r[1] for r in conn.execute("PRAGMA table_info(watcher_state)").fetchall()}
    assert "role" in ws_cols
    assert _watcher_state_pk(conn) == {"work_session_id", "role"}

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION
    conn.close()


def test_v11_db_upgrades_noise_count_and_resets_useful(tmp_path: Path):
    path = tmp_path / "v11.sqlite"
    _build_v11_db(path)

    raw = sqlite3.connect(str(path))
    now_ts = int(time.time())
    raw.execute(
        "INSERT INTO memories "
        "(name, description, body, kind, scope, project_slug, created_ts, "
        " surface_count, useful_count) "
        "VALUES ('m', '', 'b', 'hint', 'global', NULL, ?, 10, 5)",
        (now_ts,),
    )
    raw.commit()
    raw.close()

    # Pre-migration: no noise_count column.
    raw = sqlite3.connect(str(path))
    cols_before = {r[1] for r in raw.execute("PRAGMA table_info(memories)").fetchall()}
    assert "noise_count" not in cols_before
    raw.close()

    # Migrate.
    conn = db.connect(path)
    row = conn.execute(
        "SELECT surface_count, useful_count, noise_count FROM memories WHERE name = 'm'"
    ).fetchone()
    assert row["noise_count"] == 0      # new column, default
    assert row["useful_count"] == 0     # legacy counter wiped
    assert row["surface_count"] == 10   # telemetry preserved
    conn.close()


def test_v11_watcher_state_rows_become_formation(tmp_path: Path):
    path = tmp_path / "v11.sqlite"
    _build_v11_db(path)

    raw = sqlite3.connect(str(path))
    now_ts = int(time.time())
    raw.execute(
        "INSERT INTO watcher_state "
        "(work_session_id, watcher_session_id, transcript_path, last_line_read, "
        " last_checked_ts, cwd, created_ts, last_tick_ts, fail_streak) "
        "VALUES ('sess-A', 'watch-A', '/t/a.jsonl', 42, ?, '/cwd', ?, ?, 1)",
        (now_ts, now_ts, now_ts),
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)
    assert _watcher_state_pk(conn) == {"work_session_id", "role"}
    row = conn.execute(
        "SELECT role, last_line_read, fail_streak "
        "FROM watcher_state WHERE work_session_id = 'sess-A'"
    ).fetchone()
    assert row["role"] == "formation"          # existing rows default to formation
    assert row["last_line_read"] == 42         # state preserved
    assert row["fail_streak"] == 1
    # v15 then drops the resume-era column on the way through.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(watcher_state)").fetchall()}
    assert "watcher_session_id" not in cols
    conn.close()


def test_two_roles_coexist_per_session(tmp_path: Path):
    """(work_session_id, role) PK lets formation + eval rows share a session."""
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
    now_ts = int(time.time())
    for role in ("formation", "eval"):
        conn.execute(
            "INSERT INTO watcher_state "
            "(work_session_id, role, transcript_path, last_checked_ts, created_ts) "
            "VALUES ('sess-B', ?, '/t/b.jsonl', ?, ?)",
            (role, now_ts, now_ts),
        )
    n = conn.execute(
        "SELECT COUNT(*) FROM watcher_state WHERE work_session_id = 'sess-B'"
    ).fetchone()[0]
    assert n == 2
    conn.close()


def test_role_check_rejects_bogus_value(tmp_path: Path):
    path = tmp_path / "fresh.sqlite"
    conn = db.connect(path)
    now_ts = int(time.time())
    try:
        conn.execute(
            "INSERT INTO watcher_state "
            "(work_session_id, role, transcript_path, last_checked_ts, created_ts) "
            "VALUES ('sess-C', 'bogus', '/t/c.jsonl', ?, ?)",
            (now_ts, now_ts),
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised
