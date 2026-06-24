"""End-to-end PreToolUse handler test against a temp SQLite.

PreToolUse surfaces BOTH kinds: a `block` denies the call; a `hint` injects
additionalContext with no permissionDecision (an explicit allow would bypass
the user's permission prompts — see pretool.py).
"""

from __future__ import annotations

import io
import json
import sys
import time

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

    Defaults to kind=block; pass kind="hint" to exercise the context-only
    (no permissionDecision) surface path.
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


def _seed_demo_hints(conn) -> None:
    """Insert the hint memories the surfacing tests assert against.

    These used to come from `engram seed` (since removed); the tests own
    their fixtures now.
    """
    _seed_token_memory(
        conn, "psql replica is read-only",
        "psql -h replica.internal connects to a PostgreSQL production replica. "
        "SELECT-only — never attempt writes.",
        ["psql", "-h"], kind="hint",
    )
    _seed_token_memory(
        conn, "git commit uses HEREDOC for multi-line messages",
        "For commits with multi-line bodies always use the HEREDOC form.",
        ["git", "commit"], kind="hint",
    )
    _seed_token_memory(
        conn, "ssh to production: check VPN first on connection timeout",
        "If ssh deploy@production times out, the usual cause is the VPN. "
        "Check VPN state first.",
        ["ssh", "deploy@production"], kind="hint",
    )


def _seed_block_demo(conn) -> None:
    """Insert the force-push block memory the deny test asserts against."""
    _seed_token_memory(
        conn, "git push --force overwrites co-workers' commits",
        "Use git push --force-with-lease instead.",
        ["git", "push", "--force"], kind="block",
    )


def test_pretool_hint_memory_injects_without_permission_decision(temp_db, monkeypatch):
    """Seed's psql replica memory is kind=hint → context only. An explicit
    'allow' would bypass the user's permission prompts (security: hint
    triggers form autonomously and must never grant approval)."""
    _seed_demo_hints(temp_db)

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
    assert "permissionDecision" not in hso
    assert "replica" in hso["additionalContext"].lower()


def test_pretool_block_memory_denies_and_injects_context(temp_db, monkeypatch):
    """Seed's opt-in force-push memory is kind=block → denies + injects body."""
    _seed_block_demo(temp_db)

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
    _seed_demo_hints(temp_db)

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
    assert "permissionDecision" not in hso
    assert "HEREDOC" in hso["additionalContext"]


def test_pretool_surface_notice_emits_system_message(temp_db, monkeypatch):
    """ENGRAM_SURFACE_NOTICE=1 → a visible systemMessage names what surfaced."""
    _seed_demo_hints(temp_db)
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
    _seed_demo_hints(temp_db)
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
    _seed_demo_hints(temp_db)

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
    _seed_demo_hints(temp_db)

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


def test_pretool_path_glob_hint_surfaces_without_permission_decision(temp_db, monkeypatch):
    """A hint-kind path_glob memory surfaces context-only in pretool."""
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
    assert "permissionDecision" not in hso
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


def _seed_path_hint(conn, name: str, body: str, pattern: str, access_mode: str) -> int:
    """Insert a hint memory bound to a path_glob trigger with an access mode."""
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES (?, '', ?, 'hint', 'global', NULL, ?)",
        (name, body, now_ts),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, path_pattern, access_mode) "
        "VALUES (?, 'path_glob', ?, ?)",
        (mid, pattern, access_mode),
    )
    return mid


def test_pretool_write_path_hint_skips_read_fires_on_edit(temp_db, monkeypatch):
    """A write-mode path memory (issue #63) does NOT surface on a Read of the
    matching file, but DOES on an Edit of it."""
    _seed_path_hint(temp_db, "edit py rule", "Edit-only python rule", "**/*.py", "write")

    read_payload = {
        "session_id": "sess-mode",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/test-projects/myapp/main.py"},
        "tool_use_id": "tu-read",
    }
    assert _run_pretool(read_payload, monkeypatch) == {}

    edit_payload = {
        "session_id": "sess-mode",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/test-projects/myapp/main.py",
                       "old_string": "a", "new_string": "b"},
        "tool_use_id": "tu-edit",
    }
    hso = _run_pretool(edit_payload, monkeypatch)["hookSpecificOutput"]
    assert "Edit-only python rule" in hso["additionalContext"]


def test_pretool_any_path_hint_fires_on_read(temp_db, monkeypatch):
    """An 'any'-mode path memory keeps the pre-#63 fire-on-read behavior."""
    _seed_path_hint(temp_db, "any py rule", "Any-mode python rule", "**/*.py", "any")

    payload = {
        "session_id": "sess-any",
        "cwd": "/tmp/test-projects/myapp",
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/test-projects/myapp/main.py"},
        "tool_use_id": "tu-any",
    }
    hso = _run_pretool(payload, monkeypatch)["hookSpecificOutput"]
    assert "Any-mode python rule" in hso["additionalContext"]
