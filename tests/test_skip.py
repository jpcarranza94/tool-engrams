"""`engram skip <name>` — mark a memory's most recent surface as outcome='unused'."""

from __future__ import annotations

import json
import time

from toolengrams.cli import skip


def _seed_memory(conn, name: str, archived: bool = False) -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts, archived_ts) "
        "VALUES (?, '', 'b', 'hint', 'global', NULL, ?, ?)",
        (name, now_ts, now_ts if archived else None),
    )
    return cur.lastrowid


def _seed_surface(conn, session_id: str, memory_id: int, surfaced_ts: int, hook: str = "pre_tool_use") -> None:
    conn.execute(
        "INSERT INTO session_surfaces "
        "(session_id, memory_id, surfaced_ts, hook, tool_use_id, turn_at_surface) "
        "VALUES (?, ?, ?, ?, 'tu-x', 1)",
        (session_id, memory_id, surfaced_ts, hook),
    )


def _bump_session_turn(conn, session_id: str, updated_ts: int) -> None:
    conn.execute(
        "INSERT INTO session_turns (session_id, turn_count, updated_ts) VALUES (?, 1, ?)",
        (session_id, updated_ts),
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
    assert payload["resolved_via"] == "env"

    rows = temp_db.execute(
        "SELECT surfaced_ts, outcome FROM session_surfaces "
        "WHERE session_id = 'sess-1' ORDER BY surfaced_ts"
    ).fetchall()
    assert rows[0]["outcome"] is None
    assert rows[1]["outcome"] is None
    assert rows[2]["outcome"] == "unused"


def test_skip_explicit_session_id_flag_wins_over_env(temp_db, capsys, monkeypatch):
    mid = _seed_memory(temp_db, "flag-test")
    _seed_surface(temp_db, "explicit-sess", mid, 7000)

    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-sess")
    rc = skip.main(["flag-test", "--session-id", "explicit-sess"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "explicit-sess"
    assert payload["resolved_via"] == "flag"
    assert payload["action"] == "skipped"


def test_skip_no_session_errors_by_default(temp_db, capsys, monkeypatch):
    """Without --session-id, $CLAUDE_SESSION_ID, or --latest-session, must error.

    The previous version silently fell back to the newest active session,
    which could mark surfaces in an unrelated Claude window. The new
    behavior is to error so the caller is explicit about which session
    they want to mutate.
    """
    _seed_memory(temp_db, "default-no-session")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    rc = skip.main(["default-no-session"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "no_session"


def test_skip_latest_session_flag_opts_into_fallback(temp_db, capsys, monkeypatch):
    mid = _seed_memory(temp_db, "latest-target")
    now_ts = int(time.time())
    _seed_surface(temp_db, "latest-sess", mid, now_ts - 60)
    _bump_session_turn(temp_db, "latest-sess", now_ts - 60)

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    rc = skip.main(["latest-target", "--latest-session"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "latest-sess"
    assert payload["resolved_via"] == "latest-session-flag"


def test_skip_latest_session_picks_newest_active(temp_db, capsys, monkeypatch):
    """With --latest-session, two active sessions: pick the one whose
    session_turns.updated_ts is newest. Documents that this *is* picking
    by recency; if Claude calls --latest-session from a Bash subprocess,
    it might still hit the wrong session. The flag is the contract.
    """
    mid = _seed_memory(temp_db, "two-sessions")
    now_ts = int(time.time())
    _seed_surface(temp_db, "older", mid, now_ts - 1800)
    _bump_session_turn(temp_db, "older", now_ts - 1800)
    _seed_surface(temp_db, "newer", mid, now_ts - 60)
    _bump_session_turn(temp_db, "newer", now_ts - 60)

    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    rc = skip.main(["two-sessions", "--latest-session"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "newer"


def test_skip_unknown_memory_errors_before_session_resolution(
    temp_db, capsys, monkeypatch
):
    """not_found must short-circuit before session resolution so the error
    is the same regardless of whether $CLAUDE_SESSION_ID is set."""
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    rc = skip.main(["does-not-exist"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "not_found"


def test_skip_refuses_archived_memory(temp_db, capsys, monkeypatch):
    _seed_memory(temp_db, "archived-mem", archived=True)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-x")
    rc = skip.main(["archived-mem"])
    # Archived memories surface as not_found (include_archived=False);
    # marking outcome on a dead memory is meaningless.
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "not_found"


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


def test_skip_latest_session_returns_no_session_when_nothing_active(
    temp_db, capsys, monkeypatch
):
    _seed_memory(temp_db, "lonely")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    rc = skip.main(["lonely", "--latest-session"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "no_session"
