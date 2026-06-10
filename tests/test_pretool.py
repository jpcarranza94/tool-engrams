"""End-to-end PreToolUse handler test against a temp SQLite.

PreToolUse only surfaces `block`-kind memories and always denies.
`hint`-kind memories live on the PostToolUseFailure track.
"""

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
                       kind: str = "block") -> int:
    """Helper: insert a memory + token_subseq trigger.

    Defaults to kind=block since this is the pretool test; hint memories
    don't surface in pretool.
    """
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


def test_pretool_hint_memory_surfaces_as_allow(temp_db, monkeypatch):
    """Seed's psql replica memory is kind=hint → surfaces with allow (not deny)."""
    seed.main([])

    payload = {
        "session_id": "sess-abc",
        "cwd": "/tmp/test-projects/foo",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
        "tool_use_id": "tu-1",
    }
    result = _run_pretool(payload, monkeypatch)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "replica" in hso["additionalContext"].lower()


def test_pretool_block_memory_denies_and_injects_context(temp_db, monkeypatch):
    """Seed's opt-in force-push memory is kind=block → denies + injects body."""
    seed.main(["--with-block"])

    payload = {
        "session_id": "sess-xyz",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
        "tool_use_id": "tu-2",
    }
    result = _run_pretool(payload, monkeypatch)

    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "force-with-lease" in hso["additionalContext"]


def test_pretool_default_seed_never_denies_git_commit(temp_db, monkeypatch):
    """The default seed set is hint-only — a plain git commit must not be
    denied by freshly seeded demo memories (first-run safety)."""
    seed.main([])

    payload = {
        "session_id": "sess-commit",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'fix bug'"},
        "tool_use_id": "tu-commit",
    }
    result = _run_pretool(payload, monkeypatch)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "HEREDOC" in hso["additionalContext"]


def test_pretool_surface_notice_emits_system_message(temp_db, monkeypatch):
    """ENGRAM_SURFACE_NOTICE=1 → a visible systemMessage names what surfaced."""
    seed.main([])
    monkeypatch.setenv("ENGRAM_SURFACE_NOTICE", "1")

    payload = {
        "session_id": "sess-notice",
        "cwd": "/tmp/test-projects/foo",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
        "tool_use_id": "tu-notice",
    }
    result = _run_pretool(payload, monkeypatch)
    assert result["systemMessage"] == (
        "ToolEngrams surfaced: 'psql replica is read-only'"
    )


def test_pretool_no_system_message_by_default(temp_db, monkeypatch):
    seed.main([])
    monkeypatch.delenv("ENGRAM_SURFACE_NOTICE", raising=False)

    payload = {
        "session_id": "sess-no-notice",
        "cwd": "/tmp/test-projects/foo",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
        "tool_use_id": "tu-no-notice",
    }
    result = _run_pretool(payload, monkeypatch)
    assert "systemMessage" not in result


def test_pretool_subseq_match_skips_positional_arg(temp_db, monkeypatch):
    """`mycli order 12345 reassign` matches trigger
    `[mycli, order, reassign]` because subseq allows gaps."""
    _seed_token_memory(
        temp_db, "reassign rule", "Reassign body", ["mycli", "order", "reassign"]
    )

    payload = {
        "session_id": "sess-subseq",
        "cwd": "/tmp/any",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "mycli order 12345 reassign --reason Y"},
        "tool_use_id": "tu-subseq",
    }
    result = _run_pretool(payload, monkeypatch)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "Reassign body" in hso["additionalContext"]


def test_pretool_session_dedup_skips_second_time(temp_db, monkeypatch):
    """Same block surfaced twice in one session only fires once."""
    _seed_token_memory(
        temp_db, "rule", "Body of the rule", ["git", "commit"]
    )

    payload = {
        "session_id": "sess-dedup",
        "cwd": "/tmp/any",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'first'"},
        "tool_use_id": "tu-a",
    }
    first = _run_pretool(payload, monkeypatch)
    assert first["hookSpecificOutput"]["permissionDecision"] == "deny"

    payload["tool_use_id"] = "tu-b"
    second = _run_pretool(payload, monkeypatch)
    assert second == {}


def test_pretool_unknown_tool_returns_empty(temp_db, monkeypatch):
    seed.main([])

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
    seed.main([])

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


def test_pretool_path_glob_block_on_file_tool(temp_db, monkeypatch):
    """path_glob block triggers fire when a file tool targets a matching path."""
    now_ts = int(time.time())
    cur = temp_db.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES ('py rule', '', 'Python file rule', 'block', 'global', NULL, ?)",
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
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "Python file rule" in hso["additionalContext"]


def test_pretool_path_glob_hint_surfaces_as_allow(temp_db, monkeypatch):
    """A hint-kind path_glob memory surfaces in pretool with allow."""
    now_ts = int(time.time())
    cur = temp_db.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES ('py hint', '', 'Python hint body', 'hint', 'global', NULL, ?)",
        (now_ts,),
    )
    mid = cur.lastrowid
    temp_db.execute(
        "INSERT INTO triggers (memory_id, kind, path_pattern) "
        "VALUES (?, 'path_glob', '**/*.py')",
        (mid,),
    )

    payload = {
        "session_id": "sess-hint",
        "cwd": "/tmp/any",
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/any/main.py"},
        "tool_use_id": "tu-hint",
    }
    result = _run_pretool(payload, monkeypatch)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "Python hint body" in hso["additionalContext"]


def test_pretool_block_token_memory_denies(temp_db, monkeypatch):
    _seed_token_memory(
        temp_db, "git force rule", "Avoid force push", ["git", "push", "--force"]
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
