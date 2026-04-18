"""Unit tests for `engram post-tool` — PostToolUse success reinforcement."""

from __future__ import annotations

import io
import json
import time

from toolengrams.hooks import post_tool


def _seed_memory(conn, name: str = "test memory") -> int:
    cur = conn.execute(
        "INSERT INTO memories "
        "(name, description, body, type, scope, project_slug, created_ts, surface_count, useful_count) "
        "VALUES (?, '', 'body', 'reference', 'global', NULL, ?, 3, 0)",
        (name, int(time.time())),
    )
    return cur.lastrowid


def _log_surface(conn, session_id: str, memory_id: int, tool_use_id: str):
    conn.execute(
        "INSERT INTO session_surfaces (session_id, memory_id, surfaced_ts, hook, tool_use_id) "
        "VALUES (?, ?, ?, 'pre_tool_use', ?)",
        (session_id, memory_id, int(time.time()), tool_use_id),
    )


def _run(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    post_tool.main()
    return {}


# ---------- success reinforcement ----------


def test_success_bumps_useful_count(temp_db, monkeypatch, capsys):
    mid = _seed_memory(temp_db)
    _log_surface(temp_db, "sess1", mid, "tool_abc")

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "sess1",
        "tool_use_id": "tool_abc",
        "tool_name": "Bash",
        "tool_response": "On branch main\nnothing to commit",
        "is_error": False,
    })))
    post_tool.main()

    row = temp_db.execute("SELECT useful_count FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["useful_count"] == 1


def test_error_does_not_bump(temp_db, monkeypatch, capsys):
    mid = _seed_memory(temp_db)
    _log_surface(temp_db, "sess1", mid, "tool_abc")

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "sess1",
        "tool_use_id": "tool_abc",
        "tool_name": "Bash",
        "tool_response": "Exit code 1\ncommand not found",
        "is_error": True,
    })))
    post_tool.main()

    row = temp_db.execute("SELECT useful_count FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["useful_count"] == 0


def test_no_surface_for_tool_use_id(temp_db, monkeypatch, capsys):
    """No memories were surfaced for this tool call — nothing to reinforce."""
    mid = _seed_memory(temp_db)

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "sess1",
        "tool_use_id": "tool_xyz",
        "tool_name": "Bash",
        "tool_response": "ok",
        "is_error": False,
    })))
    post_tool.main()

    row = temp_db.execute("SELECT useful_count FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["useful_count"] == 0


def test_multiple_memories_all_reinforced(temp_db, monkeypatch, capsys):
    mid1 = _seed_memory(temp_db, "mem1")
    mid2 = _seed_memory(temp_db, "mem2")
    _log_surface(temp_db, "sess1", mid1, "tool_abc")
    _log_surface(temp_db, "sess1", mid2, "tool_abc")

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "sess1",
        "tool_use_id": "tool_abc",
        "tool_name": "Bash",
        "tool_response": "ok",
        "is_error": False,
    })))
    post_tool.main()

    r1 = temp_db.execute("SELECT useful_count FROM memories WHERE id = ?", (mid1,)).fetchone()
    r2 = temp_db.execute("SELECT useful_count FROM memories WHERE id = ?", (mid2,)).fetchone()
    assert r1["useful_count"] == 1
    assert r2["useful_count"] == 1


def test_stderr_exit_code_detected_as_error(temp_db, monkeypatch, capsys):
    mid = _seed_memory(temp_db)
    _log_surface(temp_db, "sess1", mid, "tool_abc")

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "sess1",
        "tool_use_id": "tool_abc",
        "tool_name": "Bash",
        "tool_response": "Exit code 127\nbash: mycli: command not found",
    })))
    post_tool.main()

    row = temp_db.execute("SELECT useful_count FROM memories WHERE id = ?", (mid,)).fetchone()
    assert row["useful_count"] == 0


# ---------- session turn counter ----------


def test_session_turns_increments_on_success(temp_db, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "turn-sess",
        "tool_use_id": "tu-1",
        "tool_name": "Bash",
        "tool_response": "ok",
        "is_error": False,
    })))
    post_tool.main()

    row = temp_db.execute(
        "SELECT turn_count FROM session_turns WHERE session_id='turn-sess'"
    ).fetchone()
    assert row["turn_count"] == 1


def test_session_turns_increments_on_error_too(temp_db, monkeypatch, capsys):
    """Turn counter tracks EVERY tool call, not just successes."""
    for i in range(3):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
            "session_id": "err-sess",
            "tool_use_id": f"tu-{i}",
            "tool_name": "Bash",
            "tool_response": "<error>boom</error>",
            "is_error": True,
        })))
        post_tool.main()

    row = temp_db.execute(
        "SELECT turn_count FROM session_turns WHERE session_id='err-sess'"
    ).fetchone()
    assert row["turn_count"] == 3


def test_session_turns_per_session_isolated(temp_db, monkeypatch, capsys):
    """Turn counter is scoped per session_id."""
    for sess, n in [("s1", 2), ("s2", 5)]:
        for i in range(n):
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
                "session_id": sess,
                "tool_use_id": f"{sess}-{i}",
                "tool_name": "Bash",
                "tool_response": "ok",
                "is_error": False,
            })))
            post_tool.main()

    rows = {r["session_id"]: r["turn_count"] for r in temp_db.execute(
        "SELECT session_id, turn_count FROM session_turns"
    ).fetchall()}
    assert rows == {"s1": 2, "s2": 5}
