"""Tests for the persistent watcher: delta formatting, response parsing, PID lifecycle."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from toolengrams import db
from toolengrams.utils import is_pid_alive
from toolengrams.watcher import (
    lifecycle,
    watcher_main,
    _cleanup,
    _format_delta,
    _get_saved_cursor,
    _is_session_alive,
    _parse_response,
    _read_lines_from,
    _save_memory,
    _update_state,
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


# ---------- session alive ----------


def test_is_session_alive_recent(tmp_path):
    f = tmp_path / "recent.jsonl"
    f.write_text("test")
    assert _is_session_alive(str(f)) is True


def test_is_session_alive_stale(tmp_path):
    f = tmp_path / "stale.jsonl"
    f.write_text("test")
    # Set mtime to 31 minutes ago.
    old_time = time.time() - (31 * 60)
    os.utime(str(f), (old_time, old_time))
    assert _is_session_alive(str(f)) is False


def test_is_session_alive_missing():
    assert _is_session_alive("/nonexistent/path.jsonl") is False


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


# ---------- PID alive ----------


def test_is_pid_alive_current_process():
    assert is_pid_alive(os.getpid()) is True


def test_is_pid_alive_nonexistent():
    # PID 99999 is extremely unlikely to exist.
    assert is_pid_alive(99999) is False


def test_is_pid_alive_zero_or_none():
    # Both falsy PID values must short-circuit to False — neither is a real
    # process and we don't want to accidentally signal pid 0 (process group).
    assert is_pid_alive(0) is False


# ---------- cursor lifecycle ----------


def test_get_saved_cursor_fresh_session(temp_db):
    """No watcher_state row → cursor starts at 0."""
    assert _get_saved_cursor("nonexistent-session") == 0


def test_get_saved_cursor_reads_persisted_value(temp_db):
    """Cursor is read from watcher_state after update."""
    now_ts = int(time.time())
    temp_db.execute(
        "INSERT INTO watcher_state (work_session_id, watcher_pid, transcript_path, "
        "last_line_read, last_checked_ts, cwd, created_ts) "
        "VALUES ('cursor-test', 123, '/tmp/t.jsonl', 0, ?, '/tmp', ?)",
        (now_ts, now_ts),
    )
    _update_state("cursor-test", "haiku-1", 42)
    assert _get_saved_cursor("cursor-test") == 42


def test_cleanup_preserves_cursor(temp_db):
    """_cleanup clears PID but keeps last_line_read for respawn."""
    now_ts = int(time.time())
    temp_db.execute(
        "INSERT INTO watcher_state (work_session_id, watcher_pid, transcript_path, "
        "last_line_read, last_checked_ts, cwd, created_ts) "
        "VALUES ('cleanup-test', 999, '/tmp/t.jsonl', 0, ?, '/tmp', ?)",
        (now_ts, now_ts),
    )
    _update_state("cleanup-test", "haiku-1", 75)

    _cleanup("cleanup-test")

    # Cursor preserved
    assert _get_saved_cursor("cleanup-test") == 75
    # PID cleared
    row = temp_db.execute(
        "SELECT watcher_pid, watcher_session_id FROM watcher_state "
        "WHERE work_session_id = 'cleanup-test'"
    ).fetchone()
    assert row["watcher_pid"] is None
    assert row["watcher_session_id"] is None


def test_respawn_preserves_cursor(temp_db):
    """INSERT ON CONFLICT (respawn) keeps existing last_line_read."""
    now_ts = int(time.time())
    # Initial spawn
    temp_db.execute(
        "INSERT INTO watcher_state (work_session_id, watcher_pid, transcript_path, "
        "last_line_read, last_checked_ts, cwd, created_ts) "
        "VALUES ('respawn-test', 111, '/tmp/t.jsonl', 0, ?, '/tmp', ?)",
        (now_ts, now_ts),
    )
    _update_state("respawn-test", "haiku-1", 50)

    # Cleanup (timeout)
    _cleanup("respawn-test")
    assert _get_saved_cursor("respawn-test") == 50

    # Respawn via ON CONFLICT — same pattern as spawn_watcher
    temp_db.execute(
        "INSERT INTO watcher_state (work_session_id, watcher_pid, transcript_path, "
        "last_line_read, last_checked_ts, cwd, created_ts) "
        "VALUES (?, ?, ?, 0, ?, ?, ?) "
        "ON CONFLICT(work_session_id) DO UPDATE SET "
        "watcher_pid = excluded.watcher_pid, "
        "transcript_path = excluded.transcript_path, "
        "last_checked_ts = excluded.last_checked_ts, "
        "cwd = excluded.cwd",
        ("respawn-test", 222, "/tmp/t.jsonl", now_ts, "/tmp", now_ts),
    )

    # Cursor still at 50, NOT reset to 0
    assert _get_saved_cursor("respawn-test") == 50
    # But PID is updated
    row = temp_db.execute(
        "SELECT watcher_pid FROM watcher_state WHERE work_session_id = 'respawn-test'"
    ).fetchone()
    assert row["watcher_pid"] == 222


def test_full_lifecycle_cursor_continuity(temp_db, tmp_path):
    """End-to-end: spawn → process → cleanup → respawn → only new lines."""
    transcript = tmp_path / "session.jsonl"
    with open(transcript, "w") as f:
        for i in range(10):
            f.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"message {i}"},
            }) + "\n")

    now_ts = int(time.time())

    # Spawn
    temp_db.execute(
        "INSERT INTO watcher_state (work_session_id, watcher_pid, transcript_path, "
        "last_line_read, last_checked_ts, cwd, created_ts) "
        "VALUES ('lifecycle', 111, ?, 0, ?, '/tmp', ?)",
        (str(transcript), now_ts, now_ts),
    )

    # Process first 5 lines
    lines = _read_lines_from(str(transcript), 0)[:5]
    delta1 = _format_delta(lines)
    assert "message 0" in delta1
    assert "message 4" in delta1
    _update_state("lifecycle", "haiku-1", 5)

    # Timeout → cleanup
    _cleanup("lifecycle")

    # Respawn
    temp_db.execute(
        "INSERT INTO watcher_state (work_session_id, watcher_pid, transcript_path, "
        "last_line_read, last_checked_ts, cwd, created_ts) "
        "VALUES (?, ?, ?, 0, ?, ?, ?) "
        "ON CONFLICT(work_session_id) DO UPDATE SET "
        "watcher_pid = excluded.watcher_pid, "
        "last_checked_ts = excluded.last_checked_ts",
        ("lifecycle", 222, str(transcript), now_ts, "/tmp", now_ts),
    )

    # Respawned watcher reads cursor from DB
    cursor = _get_saved_cursor("lifecycle")
    assert cursor == 5

    # Read only new lines
    new_lines = _read_lines_from(str(transcript), cursor)
    delta2 = _format_delta(new_lines)
    assert "message 5" in delta2
    assert "message 9" in delta2
    assert "message 0" not in delta2
    assert "message 4" not in delta2


# ---------- watcher_main loop: retry / hold-cursor wiring ----------


class _StopLoop(Exception):
    """Raised from a stubbed time.sleep to break the watcher's `while True`."""


def _user_line(text: str) -> str:
    return json.dumps(
        {"type": "message", "message": {"role": "user", "content": text}}
    ) + "\n"


# claude -p stdout envelopes that _parse_response understands.
_OK_NONE = json.dumps({"structured_output": {"action": "none"}, "session_id": "w1"})
_JUNK_PROSE = json.dumps({"result": "Sure! Happy to help.", "session_id": "w1"})


def _insert_watcher_row(session_id, transcript_path, cwd):
    now = int(time.time())
    with db.session() as conn:
        conn.execute(
            "INSERT INTO watcher_state (work_session_id, watcher_pid, "
            "transcript_path, last_line_read, last_checked_ts, cwd, created_ts) "
            "VALUES (?, 999999, ?, 0, ?, ?, ?)",
            (session_id, transcript_path, now, cwd, now),
        )


def _read_cursor(session_id):
    with db.session() as conn:
        row = conn.execute(
            "SELECT last_line_read FROM watcher_state WHERE work_session_id = ?",
            (session_id,),
        ).fetchone()
    return row["last_line_read"] if row else None


def _wire_loop(monkeypatch, tmp_path, new_fn, resume_fn, sleep_fn):
    monkeypatch.setattr(lifecycle, "LOG_PATH", tmp_path / "watcher.log")
    monkeypatch.setattr(lifecycle, "CLAUDE_BIN", "claude")
    monkeypatch.setattr(lifecycle, "_is_session_alive", lambda *a, **k: True)
    monkeypatch.setattr(lifecycle, "_claude_p_new", new_fn)
    monkeypatch.setattr(lifecycle, "_claude_p_resume", resume_fn)
    monkeypatch.setattr(lifecycle.time, "sleep", sleep_fn)


def test_watcher_loop_holds_then_advances_after_giveup(temp_db, tmp_path, monkeypatch):
    """A persistently failing window is retried in place (cursor HELD) and only
    advances after MAX_FORM_RETRIES. This is the loop wiring that the unit test
    of _retry_decision cannot prove on its own."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_user_line("do the thing"))  # one-line window
    _insert_watcher_row("s-giveup", str(transcript), "/tmp")

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("model down")

    sleeps = {"n": 0}

    def fake_sleep(_):
        sleeps["n"] += 1
        if sleeps["n"] > lifecycle.MAX_FORM_RETRIES:  # allow MAX ticks, then stop
            raise _StopLoop()

    _wire_loop(monkeypatch, tmp_path, boom, boom, fake_sleep)
    rc = watcher_main("s-giveup", str(transcript), "/tmp")

    assert rc == 0
    # Same 1-line window retried exactly MAX_FORM_RETRIES times (cursor held)...
    assert calls["n"] == lifecycle.MAX_FORM_RETRIES
    # ...then advanced past it once we gave up.
    assert _read_cursor("s-giveup") == 1


def test_watcher_loop_resets_session_on_parse_failure_retry(temp_db, tmp_path, monkeypatch):
    """After a parse failure on a --resume session, the retry must start a FRESH
    session (_claude_p_new) rather than re-feed the bad turn via _claude_p_resume."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_user_line("window one"))
    _insert_watcher_row("s-reset", str(transcript), "/tmp")

    route = []

    def fake_new(message, schema):
        route.append("new")
        return _OK_NONE          # success → advance, sets session_id w1

    def fake_resume(sid, message, schema):
        route.append("resume")
        return _JUNK_PROSE       # parse_error → hold + reset session

    sleeps = {"n": 0}

    def fake_sleep(_):
        sleeps["n"] += 1
        if sleeps["n"] == 2:     # before tick 2, a new window appears
            with open(transcript, "a") as f:
                f.write(_user_line("window two"))
        if sleeps["n"] > 3:
            raise _StopLoop()

    _wire_loop(monkeypatch, tmp_path, fake_new, fake_resume, fake_sleep)
    watcher_main("s-reset", str(transcript), "/tmp")

    # tick1: window1 via new (ok). tick2: window2 via resume (parse-fail → hold +
    # reset). tick3: window2 RETRIED via new — proving the session was reset.
    assert route == ["new", "resume", "new"]
