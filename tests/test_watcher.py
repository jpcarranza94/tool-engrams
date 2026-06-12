"""Tests for the watcher's pure helpers: delta formatting + transcript reads.

Watcher sessions call the engram CLI directly (covered by test_watcher_tick.py).
Cursor / arm / retry state lives in watcher_state (test_watcher_state.py).
"""

from __future__ import annotations

import json

from toolengrams.watcher import _format_delta, _read_lines_from


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
    assert 'AGENT: "Let me query the database."' in result


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
