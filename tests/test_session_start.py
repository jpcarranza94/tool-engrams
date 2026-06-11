"""Unit tests for the SessionStart handler — formation guidance + session tracking.

SessionStart does not spawn a persistent watcher process; it just registers
the session in watcher_state (via tick.ensure_row) so the event-driven ticks
have a cursor to track.
"""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

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
    with patch.object(session_start.tick, "ensure_row"), \
         patch.object(session_start.tick, "sweep_idle_sessions"):
        result = _run(
            {"session_id": "s1", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
            monkeypatch,
        )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "ToolEngrams: tool-bound memory" in ctx
    assert "engram remember" in ctx


def test_guidance_mentions_manual_commands(monkeypatch):
    with patch.object(session_start.tick, "ensure_row"), \
         patch.object(session_start.tick, "sweep_idle_sessions"):
        result = _run(
            {"session_id": "s2", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
            monkeypatch,
        )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "engram forget" in ctx
    assert "engram recall" in ctx


def test_session_start_tracks_real_session(temp_db, monkeypatch):
    """A real session gets a watcher_state row (cursor=0), no process spawned."""
    _run(
        {"session_id": "track-test", "cwd": "/Users/dev/projects/my-app", "source": "startup"},
        monkeypatch,
    )
    row = temp_db.execute(
        "SELECT * FROM watcher_state WHERE work_session_id = 'track-test'"
    ).fetchone()
    assert row is not None
    assert row["last_line_read"] == 0
    assert "my-app" in row["transcript_path"]


def test_session_start_skips_tracking_for_internal_cwd(temp_db, monkeypatch):
    _run(
        {"session_id": "int-test", "cwd": "/tmp/engram-consolidate-xyz", "source": "startup"},
        monkeypatch,
    )
    row = temp_db.execute(
        "SELECT * FROM watcher_state WHERE work_session_id = 'int-test'"
    ).fetchone()
    assert row is None
