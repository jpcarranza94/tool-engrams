"""Unit tests for the PostToolUseFailure hook (hint injection)."""

from __future__ import annotations

import io
import json
import sys
import time

from toolengrams.hooks import post_tool_failure


def _run(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = post_tool_failure.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def _seed_token_memory(conn, name: str, body: str, tokens: list[str], *,
                       kind: str = "hint") -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES (?, '', ?, ?, 'global', NULL, ?)",
        (name, body, kind, now_ts),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', ?, ?)",
        (mid, tokens[0], json.dumps(tokens)),
    )
    return mid


def _seed_path_memory(conn, name: str, body: str, pattern: str, *,
                      kind: str = "hint") -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES (?, '', ?, ?, 'global', NULL, ?)",
        (name, body, kind, now_ts),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, path_pattern) "
        "VALUES (?, 'path_glob', ?)",
        (mid, pattern),
    )
    return mid


# ---------- headline behavior ----------


def test_hint_memory_surfaces_on_tool_failure(temp_db, monkeypatch):
    """A hint-kind memory whose trigger matches the failed call → injected."""
    _seed_token_memory(
        temp_db, "ergdb col label", "Use column `label`, not `name`, on core_statustype.",
        ["ergdb", "-c"],
    )
    payload = {
        "session_id": "sess-ptf",
        "cwd": "/tmp/any",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "ergdb -c 'SELECT name FROM core_statustype'"},
        "tool_use_id": "tu-ptf",
        "error": "Exit code 1",
        "is_interrupt": False,
    }
    result = _run(payload, monkeypatch)

    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUseFailure"
    # PostToolUseFailure cannot block — no permissionDecision field.
    assert "permissionDecision" not in hso
    assert "column `label`" in hso["additionalContext"]


def test_block_memory_does_not_surface_on_failure(temp_db, monkeypatch):
    """Block memories only live on the PreToolUse track, not PostToolUseFailure."""
    _seed_token_memory(
        temp_db, "no force push", "never force push", ["git", "push", "--force"],
        kind="block",
    )
    payload = {
        "session_id": "sess-bf",
        "cwd": "/tmp/any",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
        "tool_use_id": "tu-bf",
        "error": "Exit code 1",
        "is_interrupt": False,
    }
    result = _run(payload, monkeypatch)
    assert result == {}


def test_interrupted_call_suppresses_hints(temp_db, monkeypatch):
    """User interruption → is_interrupt=true → no hint retrieval."""
    _seed_token_memory(
        temp_db, "pytest hint", "pytest tips body", ["pytest"]
    )
    payload = {
        "session_id": "sess-int",
        "cwd": "/tmp/any",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest tests/"},
        "tool_use_id": "tu-int",
        "error": "Tool execution canceled by user",
        "is_interrupt": True,
    }
    result = _run(payload, monkeypatch)
    assert result == {}


def test_path_glob_hint_surfaces_on_file_tool_failure(temp_db, monkeypatch):
    """Read missing .py file → hint bound to **/*.py fires."""
    _seed_path_memory(
        temp_db, "py module convention",
        "Modules in src/ are lazy-imported — missing files may be registered elsewhere.",
        "**/*.py",
    )
    payload = {
        "session_id": "sess-path",
        "cwd": "/tmp/any",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/any/missing.py"},
        "tool_use_id": "tu-rd",
        "error": "File does not exist.",
        "is_interrupt": False,
    }
    result = _run(payload, monkeypatch)
    hso = result["hookSpecificOutput"]
    assert "lazy-imported" in hso["additionalContext"]


def test_session_dedup_second_failure_no_re_inject(temp_db, monkeypatch):
    _seed_token_memory(
        temp_db, "dedup hint", "dedup body", ["mycli"],
    )
    payload = {
        "session_id": "sess-dd",
        "cwd": "/tmp/any",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "mycli do-thing"},
        "tool_use_id": "tu-a",
        "error": "Exit code 1",
        "is_interrupt": False,
    }
    first = _run(payload, monkeypatch)
    assert "hookSpecificOutput" in first

    payload["tool_use_id"] = "tu-b"
    second = _run(payload, monkeypatch)
    assert second == {}


def test_unknown_tool_returns_empty(temp_db, monkeypatch):
    payload = {
        "session_id": "sess-u",
        "cwd": "/tmp/any",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "SendMessage",
        "tool_input": {"to": "x", "message": "y"},
        "tool_use_id": "tu-u",
        "error": "something",
        "is_interrupt": False,
    }
    assert _run(payload, monkeypatch) == {}


def test_no_matching_memory_returns_empty(temp_db, monkeypatch):
    _seed_token_memory(temp_db, "other hint", "body", ["unrelated"])
    payload = {
        "session_id": "sess-nm",
        "cwd": "/tmp/any",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
        "tool_use_id": "tu-nm",
        "error": "Exit code 2",
        "is_interrupt": False,
    }
    assert _run(payload, monkeypatch) == {}


def test_invalid_json_fails_open(temp_db, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{broken"))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = post_tool_failure.main()
    assert rc == 0
    assert buf.getvalue().strip() == "{}"


def test_surface_logged_with_post_tool_use_failure_hook(temp_db, monkeypatch):
    mid = _seed_token_memory(
        temp_db, "log hint", "log body", ["mycli"],
    )
    payload = {
        "session_id": "sess-log",
        "cwd": "/tmp/any",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "mycli do"},
        "tool_use_id": "tu-log",
        "error": "Exit code 1",
        "is_interrupt": False,
    }
    _run(payload, monkeypatch)
    row = temp_db.execute(
        "SELECT hook FROM session_surfaces WHERE session_id='sess-log' AND memory_id=?",
        (mid,),
    ).fetchone()
    assert row["hook"] == "post_tool_use_failure"


def test_surface_count_bumped(temp_db, monkeypatch):
    mid = _seed_token_memory(
        temp_db, "bump hint", "body", ["mycli"],
    )
    payload = {
        "session_id": "sess-bump",
        "cwd": "/tmp/any",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "mycli x"},
        "tool_use_id": "tu-b",
        "error": "Exit code 1",
        "is_interrupt": False,
    }
    _run(payload, monkeypatch)
    row = temp_db.execute(
        "SELECT surface_count FROM memories WHERE id=?", (mid,),
    ).fetchone()
    assert row["surface_count"] == 1
