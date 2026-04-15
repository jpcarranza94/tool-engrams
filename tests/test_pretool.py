"""End-to-end PreToolUse handler test against a temp SQLite."""

from __future__ import annotations

import io
import json
import sys
import time

from toolengrams.commands import pretool, seed


def _run_pretool(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = pretool.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def test_pretool_hits_seeded_memory(temp_db, monkeypatch):
    seed.main()

    payload = {
        "session_id": "sess-abc",
        "cwd": "/tmp/test-projects/foo",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "mycli -c 'SELECT 1'"},
        "tool_use_id": "tu-1",
    }
    result = _run_pretool(payload, monkeypatch)

    hso = result.get("hookSpecificOutput")
    assert hso is not None
    assert hso["hookEventName"] == "PreToolUse"
    # Seeded mycli memory is type=reference → allow (not deny).
    # Only feedback memories with tool_head triggers get denied.
    assert hso["permissionDecision"] == "allow"
    assert "mycli" in hso["additionalContext"].lower()


def test_pretool_git_commit_surfaces_commit_memory(temp_db, monkeypatch):
    seed.main()

    payload = {
        "session_id": "sess-xyz",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'fix bug'"},
        "tool_use_id": "tu-2",
    }
    result = _run_pretool(payload, monkeypatch)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "HEREDOC" in ctx


def test_pretool_ssh_prefix_match(temp_db, monkeypatch):
    seed.main()

    payload = {
        "session_id": "sess-ssh",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ssh deploy@10.0.1.50 uptime"},
        "tool_use_id": "tu-3",
    }
    result = _run_pretool(payload, monkeypatch)

    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "VPN" in ctx or "Connection refused" in ctx


def test_pretool_session_dedup_skips_second_time(temp_db, monkeypatch):
    seed.main()

    payload = {
        "session_id": "sess-dedup",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "mycli -c 'SELECT 1'"},
        "tool_use_id": "tu-a",
    }
    first = _run_pretool(payload, monkeypatch)
    assert "hookSpecificOutput" in first

    payload["tool_use_id"] = "tu-b"
    second = _run_pretool(payload, monkeypatch)
    # Same session + same memory = already surfaced, no re-injection.
    assert second == {}


def test_pretool_unknown_tool_returns_empty(temp_db, monkeypatch):
    seed.main()

    payload = {
        "session_id": "sess-1",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "SendMessage",
        "tool_input": {"to": "teammate", "message": "hi"},
        "tool_use_id": "tu-x",
    }
    result = _run_pretool(payload, monkeypatch)
    assert result == {}


def test_pretool_no_matching_memory_returns_empty(temp_db, monkeypatch):
    seed.main()

    payload = {
        "session_id": "sess-2",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
        "tool_use_id": "tu-y",
    }
    result = _run_pretool(payload, monkeypatch)
    assert result == {}


def test_pretool_invalid_json_fails_open(temp_db, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = pretool.main()
    assert rc == 0
    assert buf.getvalue().strip() == "{}"


def test_pretool_path_glob_match_on_file_tool(temp_db, monkeypatch):
    """path_glob triggers fire when Read/Edit/Write targets a matching path."""
    # Insert a memory with a path_glob trigger manually.
    now_ts = int(time.time())
    cur = temp_db.execute(
        "INSERT INTO memories (name, description, body, type, scope, project_slug, created_ts) "
        "VALUES ('py rule', '', 'Python file rule', 'feedback', 'global', NULL, ?)",
        (now_ts,),
    )
    mid = cur.lastrowid
    temp_db.execute(
        "INSERT INTO triggers (memory_id, kind, path_pattern) "
        "VALUES (?, 'path_glob', '**/*.py')",
        (mid,),
    )

    payload = {
        "session_id": "sess-path",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/test-projects/myapp/main.py"},
        "tool_use_id": "tu-path",
    }
    result = _run_pretool(payload, monkeypatch)
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Python file rule" in ctx
