"""End-to-end: post_tool_failure hint → next same-first_token success bumps useful_count."""

from __future__ import annotations

import io
import json
import sys
import time

from toolengrams.hooks import post_tool, post_tool_failure, pretool


def _seed_hint(conn, name: str, body: str, tokens: list[str]) -> int:
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


def _run_hook(hook_module, payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = hook_module.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def test_failure_hint_credited_on_next_same_first_token_success(temp_db, monkeypatch):
    _seed_hint(
        temp_db, "git-force-push",
        "Without this memory, Claude would force push and overwrite teammates' commits.",
        ["git", "push", "--force"],
    )

    # 1. The failed `git push --force` surfaces the hint via post_tool_failure.
    _run_hook(post_tool_failure, {
        "session_id": "sess-X",
        "cwd": "/tmp/x",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
        "tool_use_id": "tu-1",
        "error": "Exit code 1",
        "is_interrupt": False,
    }, monkeypatch)

    # useful_count is still 0 — the failure call surfaced but didn't succeed.
    row = temp_db.execute(
        "SELECT useful_count FROM memories WHERE name = 'git-force-push'"
    ).fetchone()
    assert row["useful_count"] == 0

    # 2. The next call: `git push --force-with-lease` succeeds.
    _run_hook(post_tool, {
        "session_id": "sess-X",
        "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force-with-lease origin main"},
        "tool_use_id": "tu-2",
        "tool_response": "Everything up-to-date",
    }, monkeypatch)

    # useful_count must now be 1 — the prior failure surface got credited.
    row = temp_db.execute(
        "SELECT useful_count FROM memories WHERE name = 'git-force-push'"
    ).fetchone()
    assert row["useful_count"] == 1

    # The session_surfaces row should be marked outcome='helpful'.
    surface = temp_db.execute(
        "SELECT outcome FROM session_surfaces "
        "WHERE session_id = 'sess-X' AND hook = 'post_tool_use_failure'"
    ).fetchone()
    assert surface["outcome"] == "helpful"


def test_failure_hint_not_credited_for_different_first_token(temp_db, monkeypatch):
    _seed_hint(
        temp_db, "git-only", "Hint about git.", ["git", "push"],
    )

    _run_hook(post_tool_failure, {
        "session_id": "sess-Y",
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main"},
        "tool_use_id": "tu-1",
        "error": "Exit code 1",
        "is_interrupt": False,
    }, monkeypatch)

    # Next call is `ls -la` — different first_token, must NOT credit.
    _run_hook(post_tool, {
        "session_id": "sess-Y",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "tool_use_id": "tu-2",
        "tool_response": "drwxr-xr-x ...",
    }, monkeypatch)

    row = temp_db.execute(
        "SELECT useful_count FROM memories WHERE name = 'git-only'"
    ).fetchone()
    assert row["useful_count"] == 0


def test_failure_hint_not_credited_twice(temp_db, monkeypatch):
    _seed_hint(temp_db, "h", "body", ["git", "push"])

    _run_hook(post_tool_failure, {
        "session_id": "sess-Z",
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main"},
        "tool_use_id": "tu-1",
        "error": "Exit code 1",
        "is_interrupt": False,
    }, monkeypatch)

    # First success — bump.
    _run_hook(post_tool, {
        "session_id": "sess-Z",
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main"},
        "tool_use_id": "tu-2",
        "tool_response": "ok",
    }, monkeypatch)

    # Second success — surface row is already marked, must NOT bump again.
    _run_hook(post_tool, {
        "session_id": "sess-Z",
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin develop"},
        "tool_use_id": "tu-3",
        "tool_response": "ok",
    }, monkeypatch)

    row = temp_db.execute(
        "SELECT useful_count FROM memories WHERE name = 'h'"
    ).fetchone()
    assert row["useful_count"] == 1


def test_pretool_hint_surface_marked_helpful_on_success(temp_db, monkeypatch):
    """Existing pre_tool_use reinforcement path also writes outcome='helpful'."""
    _seed_hint(temp_db, "ph", "psql replica hint", ["psql", "-h"])

    _run_hook(pretool, {
        "session_id": "sess-PT",
        "cwd": "/tmp/x",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
        "tool_use_id": "tu-1",
    }, monkeypatch)

    _run_hook(post_tool, {
        "session_id": "sess-PT",
        "tool_name": "Bash",
        "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
        "tool_use_id": "tu-1",
        "tool_response": "1 row",
    }, monkeypatch)

    surface = temp_db.execute(
        "SELECT outcome FROM session_surfaces "
        "WHERE session_id = 'sess-PT' AND hook = 'pre_tool_use'"
    ).fetchone()
    assert surface["outcome"] == "helpful"
