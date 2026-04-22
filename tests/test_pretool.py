"""End-to-end PreToolUse handler test against a temp SQLite."""

from __future__ import annotations

import io
import json
import sys
import time

from toolengrams.cli import seed
from toolengrams.hooks import pretool


def _run_pretool(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = pretool.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def _seed_token_memory(conn, name: str, body: str, tokens: list[str], *,
                       type_: str = "reference") -> int:
    """Helper: insert a memory + token_subseq trigger."""
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, type, scope, project_slug, created_ts) "
        "VALUES (?, '', ?, ?, 'global', NULL, ?)",
        (name, body, type_, now_ts),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', ?, ?)",
        (mid, tokens[0], json.dumps(tokens)),
    )
    return mid


def test_pretool_hits_seeded_memory(temp_db, monkeypatch):
    seed.main()

    payload = {
        "session_id": "sess-abc",
        "cwd": "/tmp/test-projects/foo",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
        "tool_use_id": "tu-1",
    }
    result = _run_pretool(payload, monkeypatch)

    hso = result.get("hookSpecificOutput")
    assert hso is not None
    assert hso["hookEventName"] == "PreToolUse"
    # Seeded psql replica memory is type=reference → allow (not deny).
    assert hso["permissionDecision"] == "allow"
    assert "replica" in hso["additionalContext"].lower()


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


def test_pretool_subseq_match_skips_positional_arg(temp_db, monkeypatch):
    """The v2 flagship case: `ergeon order 12345 reassign` matches
    trigger `[ergeon, order, reassign]` because subseq allows gaps."""
    _seed_token_memory(
        temp_db, "reassign rule", "Reassign body", ["ergeon", "order", "reassign"]
    )

    payload = {
        "session_id": "sess-subseq",
        "cwd": "/tmp/any",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ergeon order 12345 reassign --reason Y"},
        "tool_use_id": "tu-subseq",
    }
    result = _run_pretool(payload, monkeypatch)
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "Reassign body" in ctx


def test_pretool_session_dedup_skips_second_time(temp_db, monkeypatch):
    seed.main()

    payload = {
        "session_id": "sess-dedup",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
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


def test_pretool_feedback_memory_denies(temp_db, monkeypatch):
    """feedback-type token_subseq match → permissionDecision: deny."""
    _seed_token_memory(
        temp_db, "git force rule", "Avoid force push", ["git", "push", "--force"],
        type_="feedback",
    )
    payload = {
        "session_id": "sess-deny",
        "cwd": "/tmp/any",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
        "tool_use_id": "tu-deny",
    }
    result = _run_pretool(payload, monkeypatch)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_logs_turn_at_surface(temp_db, monkeypatch):
    """Logged surfaces should record the current session turn."""
    mem = _seed_token_memory(
        temp_db, "turn rule", "Turn body", ["git", "status"]
    )
    now_ts = int(time.time())
    temp_db.execute(
        "INSERT INTO session_turns (session_id, turn_count, updated_ts) "
        "VALUES ('sess-turn', 4, ?)",
        (now_ts,),
    )

    payload = {
        "session_id": "sess-turn",
        "cwd": "/tmp/any",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "tu-turn",
    }
    _run_pretool(payload, monkeypatch)

    row = temp_db.execute(
        "SELECT turn_at_surface FROM session_surfaces "
        "WHERE session_id='sess-turn' AND memory_id=?",
        (mem,),
    ).fetchone()
    assert row["turn_at_surface"] == 4
