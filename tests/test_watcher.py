"""Tests for the watcher's pure helpers: delta formatting, response parsing,
and memory saving.

Cursor / arm / retry state lives in watcher_state and is covered by
test_watcher_state.py; the event-driven tick is covered by test_watcher_tick.py.
"""

from __future__ import annotations

import json

from toolengrams.watcher import (
    _format_delta,
    _parse_response,
    _read_lines_from,
    _save_memory,
)


# ---------- delta formatting ----------


def test_format_delta_user_message():
    line = json.dumps({
        "type": "message",
        "message": {"role": "user", "content": "can you check the db?"},
    })
    result = _format_delta([line])
    assert 'USER: "can you check the db?"' in result


def test_format_delta_assistant_text():
    line = json.dumps({
        "type": "message",
        "message": {"role": "assistant", "content": "Let me query the database."},
    })
    result = _format_delta([line])
    assert 'CLAUDE: "Let me query the database."' in result


def test_format_delta_tool_use_bash():
    line = json.dumps({
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ergdb -c \"SELECT * FROM users\""}}
            ],
        },
    })
    result = _format_delta([line])
    assert "TOOL (Bash): ergdb -c" in result


def test_format_delta_tool_use_edit():
    line = json.dumps({
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/src/main.py"}}
            ],
        },
    })
    result = _format_delta([line])
    assert "TOOL (Edit): /repo/src/main.py" in result


def test_format_delta_tool_result_truncated():
    long_output = "x" * 500
    line = json.dumps({
        "type": "message",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "content": long_output, "tool_use_id": "t1"}
            ],
        },
    })
    result = _format_delta([line])
    assert "RESULT:" in result
    # The output should be truncated to 200 chars.
    result_line = [l for l in result.splitlines() if l.startswith("RESULT:")][0]
    # 200 chars of content + "RESULT: " prefix
    assert len(result_line) <= 208


def test_format_delta_error_preserved():
    error_output = "ERROR: column \"name\" does not exist -- " + "x" * 500
    line = json.dumps({
        "type": "message",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "content": error_output, "is_error": True, "tool_use_id": "t1"}
            ],
        },
    })
    result = _format_delta([line])
    assert "RESULT:" in result
    # Error messages should be preserved in full (not truncated to 200 chars).
    assert "column \"name\" does not exist" in result
    # Full content should be present (more than 200 chars of original).
    assert len(result) > 300


def test_format_delta_skips_queue_operations():
    line = json.dumps({"type": "queue-operation", "data": "something"})
    result = _format_delta([line])
    assert result.strip() == ""


def test_format_delta_skips_attachments():
    line = json.dumps({"type": "attachment", "data": "something"})
    result = _format_delta([line])
    assert result.strip() == ""


def test_format_delta_skips_system_reminders():
    line = json.dumps({
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Here is a <system-reminder> block that should be skipped"}
            ],
        },
    })
    result = _format_delta([line])
    assert result.strip() == ""


def test_format_delta_skips_last_prompt():
    line = json.dumps({"type": "last-prompt", "data": "something"})
    result = _format_delta([line])
    assert result.strip() == ""


def test_format_delta_user_message_capped_at_500():
    long_msg = "a" * 1000
    line = json.dumps({
        "type": "message",
        "message": {"role": "user", "content": long_msg},
    })
    result = _format_delta([line])
    # USER: "..." — the content inside should be capped at 500.
    assert len(result) < 520


def test_format_delta_assistant_text_capped_at_300():
    long_msg = "b" * 1000
    line = json.dumps({
        "type": "message",
        "message": {"role": "assistant", "content": long_msg},
    })
    result = _format_delta([line])
    assert len(result) < 320


# ---------- read lines from offset ----------


def test_read_lines_from_offset(tmp_path):
    f = tmp_path / "test.jsonl"
    lines = ['{"line": 0}\n', '{"line": 1}\n', '{"line": 2}\n', '{"line": 3}\n']
    f.write_text("".join(lines))
    result = _read_lines_from(str(f), 2)
    assert len(result) == 2
    assert json.loads(result[0])["line"] == 2
    assert json.loads(result[1])["line"] == 3


def test_read_lines_from_start(tmp_path):
    f = tmp_path / "test.jsonl"
    lines = ['{"line": 0}\n', '{"line": 1}\n']
    f.write_text("".join(lines))
    result = _read_lines_from(str(f), 0)
    assert len(result) == 2


def test_read_lines_from_missing_file():
    result = _read_lines_from("/nonexistent/path.jsonl", 0)
    assert result == []


# ---------- parse response ----------


def test_parse_response_none():
    stdout = json.dumps({"result": '{"action": "none"}'})
    response = _parse_response(stdout)
    assert response is not None
    assert response["action"] == "none"
    assert response.get("memories") is None


def test_parse_response_create():
    inner = json.dumps({
        "action": "create",
        "memories": [{
            "name": "test-mem",
            "body": "Without this memory, Claude would fail.",
            "kind": "block",
            "scope": "project",
            "triggers": ["test cmd"],
            "paths": [],
        }],
    })
    stdout = json.dumps({"result": inner})
    response = _parse_response(stdout)
    assert response["action"] == "create"
    assert len(response["memories"]) == 1
    assert response["memories"][0]["name"] == "test-mem"


def test_parse_response_empty():
    response = _parse_response("")
    assert response is None


def test_parse_response_garbage():
    response = _parse_response("this is not json")
    assert response is None


# ---------- save memory ----------


def test_save_memory_uses_cwd_for_project_slug(temp_db, capsys):
    """The watcher passes its work_session cwd to remember via --project-cwd
    so the resulting memory binds to the user's project slug, not the
    watcher subprocess's own cwd (which is wherever launchd left it).
    """
    _save_memory(
        {
            "name": "test-save",
            "body": "Without this memory, Claude would use `docker build` wrong.",
            "kind": "hint",
            "scope": "project",
            "triggers": ["docker build"],
            "paths": [],
        },
        cwd="/tmp/test-project",
    )
    row = temp_db.execute(
        "SELECT name, body, project_slug FROM memories WHERE name = 'test-save'"
    ).fetchone()
    assert row is not None
    assert "docker build" in row["body"]
    assert row["project_slug"] == "-tmp-test-project"


def test_save_memory_skips_no_triggers(temp_db, capsys):
    _save_memory(
        {
            "name": "no-triggers",
            "body": "Without this memory...",
            "kind": "hint",
            "scope": "project",
            "triggers": [],
            "paths": [],
        },
        cwd="/tmp/test-project",
    )
    row = temp_db.execute(
        "SELECT COUNT(*) AS c FROM memories WHERE name = 'no-triggers'"
    ).fetchone()
    assert row["c"] == 0


def test_save_memory_skips_no_name(temp_db, capsys):
    _save_memory(
        {
            "name": "",
            "body": "Without this memory...",
            "kind": "hint",
            "scope": "project",
            "triggers": ["test cmd"],
            "paths": [],
        },
        cwd="/tmp/test-project",
    )
    count = temp_db.execute("SELECT COUNT(*) AS c FROM memories").fetchone()
    assert count["c"] == 0
