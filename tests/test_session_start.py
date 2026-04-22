"""Unit tests for the SessionStart handler — formation guidance injection + watcher spawn."""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

from toolengrams import db
from toolengrams.hooks import session_start


def _run(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = session_start.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def test_session_start_emits_guidance(monkeypatch):
    with patch.object(session_start, "spawn_watcher"):
        result = _run(
            {"session_id": "s1", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
            monkeypatch,
        )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "ToolEngrams: tool-bound memory" in ctx
    assert "engram remember" in ctx


def test_guidance_mentions_manual_commands(monkeypatch):
    with patch.object(session_start, "spawn_watcher"):
        result = _run(
            {"session_id": "s2", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
            monkeypatch,
        )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "engram forget" in ctx
    assert "engram recall" in ctx


def test_session_start_spawns_watcher_on_startup(temp_db, monkeypatch):
    """SessionStart with source=startup should spawn a watcher and create a watcher_state row."""
    spawned = []

    def mock_spawn(session_id, transcript_path, cwd):
        spawned.append((session_id, transcript_path, cwd))
        # Simulate what real spawn_watcher does: insert a row.
        import time
        now_ts = int(time.time())
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO watcher_state "
                "(work_session_id, watcher_pid, transcript_path, "
                " last_line_read, last_checked_ts, cwd, created_ts) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                (session_id, 12345, transcript_path, now_ts, cwd, now_ts),
            )
        finally:
            conn.close()

    with patch.object(session_start, "spawn_watcher", side_effect=mock_spawn):
        _run(
            {"session_id": "watcher-test", "cwd": "/tmp/myproject", "source": "startup"},
            monkeypatch,
        )

    assert len(spawned) == 1
    assert spawned[0][0] == "watcher-test"
    assert "/tmp/myproject" in spawned[0][2]

    # Verify watcher_state row was created.
    row = temp_db.execute(
        "SELECT * FROM watcher_state WHERE work_session_id = 'watcher-test'"
    ).fetchone()
    assert row is not None
    assert row["watcher_pid"] == 12345


def test_session_start_spawns_watcher_on_clear(monkeypatch):
    """SessionStart with source=clear should spawn a watcher (session continues)."""
    spawned = []

    def mock_spawn(session_id, transcript_path, cwd):
        spawned.append(True)

    with patch.object(session_start, "spawn_watcher", side_effect=mock_spawn):
        _run(
            {"session_id": "s-clear", "cwd": "/tmp/foo", "source": "clear"},
            monkeypatch,
        )

    assert len(spawned) == 1
