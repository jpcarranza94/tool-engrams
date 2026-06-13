"""Unit tests for `engram post-tool` — turn counter + recovery tick.

post_tool does not credit memories on success. The single writer of
useful_count is the evaluation watcher (`engram judge`), so every assertion
here is that a success leaves useful_count untouched.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import time

from toolengrams.hooks import post_tool

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex" / "hooks"


def _seed_memory(conn, name: str = "test memory") -> int:
    cur = conn.execute(
        "INSERT INTO memories "
        "(name, description, body, kind, scope, project_slug, created_ts, surface_count, useful_count) "
        "VALUES (?, '', 'body', 'hint', 'global', NULL, ?, 3, 0)",
        (name, int(time.time())),
    )
    return cur.lastrowid


def _seed_token_memory(conn, name: str, body: str, tokens: list[str]) -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES (?, '', ?, 'hint', 'global', NULL, ?)",
        (name, body, now_ts),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', ?, ?)",
        (mid, tokens[0], json.dumps(tokens)),
    )
    return mid


def _log_surface(conn, session_id: str, memory_id: int, tool_use_id: str):
    conn.execute(
        "INSERT INTO session_surfaces (session_id, memory_id, surfaced_ts, hook, tool_use_id) "
        "VALUES (?, ?, ?, 'pre_tool_use', ?)",
        (session_id, memory_id, int(time.time()), tool_use_id),
    )


def _run(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    rc = post_tool.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


# ---------- success reinforcement ----------


def test_success_does_not_credit(temp_db, monkeypatch, capsys):
    """A successful call does not bump useful_count."""
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

    row = temp_db.execute(
        "SELECT useful_count, noise_count FROM memories WHERE id = ?", (mid,)
    ).fetchone()
    assert row["useful_count"] == 0
    assert row["noise_count"] == 0
    # The surface row is left for the eval watcher to judge — still NULL.
    s = temp_db.execute(
        "SELECT outcome FROM session_surfaces WHERE memory_id = ?", (mid,)
    ).fetchone()
    assert s["outcome"] is None


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


def test_success_credits_no_surfaced_memory(temp_db, monkeypatch, capsys):
    """Multiple pre-tool surfaces on a successful call: none are credited."""
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
    assert r1["useful_count"] == 0
    assert r2["useful_count"] == 0


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


def test_codex_inline_failure_surface_on_post_tool(temp_db, monkeypatch):
    mid = _seed_token_memory(
        temp_db, "missing path recovery", "Check the path before retrying.", ["ls"]
    )
    payload = json.loads((FIXTURE_DIR / "post_tool_use_failure.json").read_text())
    monkeypatch.setattr(post_tool.tick, "arm", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)

    rc = post_tool.main("codex")

    assert rc == 0
    result = json.loads(buf.getvalue())
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "Check the path" in hso["additionalContext"]
    row = temp_db.execute(
        "SELECT hook FROM session_surfaces WHERE session_id='codex-sess-fail' "
        "AND memory_id=?",
        (mid,),
    ).fetchone()
    assert row["hook"] == "post_tool_use_failure"


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
