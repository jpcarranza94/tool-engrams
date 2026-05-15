"""`engram skip <name>` — mark a memory's most recent surface as outcome='unused'."""

from __future__ import annotations

import json
import time

from toolengrams.cli import skip


def _seed_memory(conn, name: str) -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES (?, '', 'b', 'hint', 'global', NULL, ?)",
        (name, now_ts),
    )
    return cur.lastrowid


def _seed_surface(conn, session_id: str, memory_id: int, surfaced_ts: int, hook: str = "pre_tool_use") -> None:
    conn.execute(
        "INSERT INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES (?, ?, ?, ?, 'tu-x', 1)",
        (session_id, memory_id, surfaced_ts, hook),
    )


def test_skip_marks_latest_unmarked_surface_unused(temp_db, capsys, monkeypatch):
    mid = _seed_memory(temp_db, "noisy-mem")
    _seed_surface(temp_db, "sess-1", mid, 1000)
    _seed_surface(temp_db, "sess-1", mid, 2000)
    _seed_surface(temp_db, "sess-1", mid, 3000)  # latest

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-1")
    rc = skip.main(["noisy-mem"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "skipped"
    assert payload["surfaced_ts"] == 3000
    assert payload["outcome"] == "unused"

    # Only the 3000-ts row should be marked; earlier rows stay NULL.
    rows = temp_db.execute(
        "SELECT surfaced_ts, outcome FROM session_surfaces "
        "WHERE session_id = 'sess-1' ORDER BY surfaced_ts"
    ).fetchall()
    assert rows[0]["outcome"] is None  # 1000
    assert rows[1]["outcome"] is None  # 2000
    assert rows[2]["outcome"] == "unused"  # 3000


def test_skip_falls_back_to_active_session(temp_db, capsys, monkeypatch):
    mid = _seed_memory(temp_db, "fallback-mem")
    now_ts = int(time.time())
    _seed_surface(temp_db, "active-sess", mid, now_ts - 60)

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    rc = skip.main(["fallback-mem"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "active-sess"
    assert payload["action"] == "skipped"


def test_skip_no_active_session_errors(temp_db, capsys, monkeypatch):
    _seed_memory(temp_db, "no-surface")  # memory exists but no surfaces

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    rc = skip.main(["no-surface"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "no_active_session"


def test_skip_noop_when_already_marked(temp_db, capsys, monkeypatch):
    mid = _seed_memory(temp_db, "already-marked")
    _seed_surface(temp_db, "sess-2", mid, 5000)
    temp_db.execute(
        "UPDATE session_surfaces SET outcome = 'helpful' "
        "WHERE session_id = 'sess-2' AND memory_id = ?",
        (mid,),
    )

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-2")
    rc = skip.main(["already-marked"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "noop"
    assert payload["reason"] == "no_unmarked_surface_in_session"


def test_skip_unknown_memory(temp_db, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-x")
    rc = skip.main(["does-not-exist"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "not_found"


def test_skip_explicit_session_id_flag(temp_db, capsys, monkeypatch):
    mid = _seed_memory(temp_db, "flag-test")
    _seed_surface(temp_db, "explicit-sess", mid, 7000)

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    rc = skip.main(["flag-test", "--session-id", "explicit-sess"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "explicit-sess"
    assert payload["action"] == "skipped"
