"""Unit tests for the PostToolUse failure-subset handler."""

from __future__ import annotations

import io
import json
import sys
import time

from memctl.commands import post_failure


def _run(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = post_failure.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def _insert_error_memory(conn, *, name, body, tool, head, substring):
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, type, scope, created_ts) "
        "VALUES (?, '', ?, 'reference', 'global', ?)",
        (name, body, now),
    )
    mid = cur.lastrowid
    head_joined = " ".join(head) if head else ""
    conn.execute(
        "INSERT INTO triggers "
        "(memory_id, kind, tool_name, head_joined, head_length, error_substring) "
        "VALUES (?, 'error_contains', ?, ?, ?, ?)",
        (mid, tool, head_joined, len(head or []), substring),
    )
    return mid


def test_post_failure_matches_error_substring(temp_db, monkeypatch):
    _insert_error_memory(
        temp_db,
        name="ssh timeout → check VPN",
        body="When ssh to production fails with Connection refused, the VPN is probably down.",
        tool="Bash",
        head=["ssh"],
        substring="Connection refused",
    )
    result = _run(
        {
            "session_id": "e1",
            "cwd": "/tmp",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ssh deploy@10.0.1.50 uptime"},
            "tool_response": {
                "stderr": "ssh: connect to host 10.0.1.50 port 22: Connection refused",
                "stdout": "",
                "is_error": True,
            },
            "tool_use_id": "t1",
        },
        monkeypatch,
    )
    assert "VPN is probably down" in result["hookSpecificOutput"]["additionalContext"]


def test_post_failure_no_error_returns_empty(temp_db, monkeypatch):
    _insert_error_memory(
        temp_db, name="m", body="body", tool="Bash", head=["ssh"], substring="refused"
    )
    # Successful call — no error_text, no injection
    result = _run(
        {
            "session_id": "e2",
            "cwd": "/tmp",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ssh host uptime"},
            "tool_response": {"stdout": "up 5 days", "stderr": "", "is_error": False},
            "tool_use_id": "t2",
        },
        monkeypatch,
    )
    assert result == {}


def test_post_failure_wrong_tool_head_skipped(temp_db, monkeypatch):
    _insert_error_memory(
        temp_db, name="m", body="body", tool="Bash", head=["ssh"], substring="refused"
    )
    result = _run(
        {
            "session_id": "e3",
            "cwd": "/tmp",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git pull"},
            "tool_response": {"stderr": "Connection refused", "stdout": "", "is_error": True},
            "tool_use_id": "t3",
        },
        monkeypatch,
    )
    assert result == {}


def test_post_failure_unknown_tool_returns_empty(temp_db, monkeypatch):
    result = _run(
        {
            "session_id": "e4",
            "cwd": "/tmp",
            "hook_event_name": "PostToolUse",
            "tool_name": "SendMessage",
            "tool_input": {"to": "x", "message": "y"},
            "tool_response": {"is_error": True, "stderr": "boom"},
            "tool_use_id": "t4",
        },
        monkeypatch,
    )
    assert result == {}


def test_post_failure_dedup_same_session(temp_db, monkeypatch):
    _insert_error_memory(
        temp_db, name="m", body="hint", tool="Bash", head=["ssh"], substring="refused"
    )
    payload = {
        "session_id": "e-dedup",
        "cwd": "/tmp",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ssh host uptime"},
        "tool_response": {"is_error": True, "stderr": "Connection refused"},
        "tool_use_id": "t-a",
    }
    first = _run(payload, monkeypatch)
    assert "hint" in first["hookSpecificOutput"]["additionalContext"]

    payload["tool_use_id"] = "t-b"
    second = _run(payload, monkeypatch)
    assert second == {}
