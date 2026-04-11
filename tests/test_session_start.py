"""Unit tests for the SessionStart handler."""

from __future__ import annotations

import io
import json
import sys
import time

from toolengrams.commands import session_start


def _run(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = session_start.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def _insert_memory(conn, *, name, body, type_="user", pinned=False, scope="global"):
    now = int(time.time())
    conn.execute(
        "INSERT INTO memories (name, description, body, type, scope, created_ts, pinned) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, "", body, type_, scope, now, 1 if pinned else 0),
    )


def test_session_start_injects_user_memory(temp_db, monkeypatch):
    _insert_memory(temp_db, name="identity: prefer terse", body="User prefers short replies.")
    result = _run(
        {"session_id": "s1", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
        monkeypatch,
    )
    hso = result.get("hookSpecificOutput")
    assert hso is not None
    assert hso["hookEventName"] == "SessionStart"
    assert "User prefers short replies" in hso["additionalContext"]


def test_session_start_injects_pinned_non_user_memory(temp_db, monkeypatch):
    _insert_memory(temp_db, name="pinned fact", body="critical detail", type_="reference", pinned=True)
    result = _run(
        {"session_id": "s2", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
        monkeypatch,
    )
    assert "critical detail" in result["hookSpecificOutput"]["additionalContext"]


def test_session_start_skips_non_user_unpinned(temp_db, monkeypatch):
    _insert_memory(temp_db, name="project fact", body="IRRELEVANT_AT_START", type_="project", pinned=False)
    result = _run(
        {"session_id": "s3", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
        monkeypatch,
    )
    assert result == {}


def test_session_start_empty_db_returns_empty(temp_db, monkeypatch):
    result = _run(
        {"session_id": "s4", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
        monkeypatch,
    )
    assert result == {}


def test_session_start_logs_surfaces(temp_db, monkeypatch):
    _insert_memory(temp_db, name="m", body="hello")
    _run(
        {"session_id": "s-log", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
        monkeypatch,
    )
    rows = temp_db.execute(
        "SELECT memory_id, hook FROM session_surfaces WHERE session_id = ?",
        ("s-log",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["hook"] == "session_start"
