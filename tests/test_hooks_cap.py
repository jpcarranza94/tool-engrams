"""Per-call memory cap (ENGRAM_MAX_MEMORIES_PER_CALL).

Both pretool and post_tool_failure surface at most N memories per call.
Default N=2. Blocks always preserved at the pretool layer; hints trimmed
by score order.
"""

from __future__ import annotations

import io
import json
import sys
import time

from toolengrams.hooks import post_tool_failure, pretool


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


def _seed_block(conn, name: str, body: str, tokens: list[str]) -> int:
    now_ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES (?, '', ?, 'block', 'global', NULL, ?)",
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


def test_pretool_caps_hints_at_default_two(temp_db, monkeypatch):
    # 4 hints all match `git status`
    _seed_hint(temp_db, "hint-a", "Hint body A about git", ["git", "status"])
    _seed_hint(temp_db, "hint-b", "Hint body B about git", ["git", "status"])
    _seed_hint(temp_db, "hint-c", "Hint body C about git", ["git", "status"])
    _seed_hint(temp_db, "hint-d", "Hint body D about git", ["git", "status"])

    payload = {
        "session_id": "sess-cap-1",
        "cwd": "/tmp/x",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "tu-cap-1",
    }
    result = _run_hook(pretool, payload, monkeypatch)
    ctx = result["hookSpecificOutput"]["additionalContext"]
    # At most 2 of the 4 hint bodies should be present.
    present = sum(1 for letter in "ABCD" if f"Hint body {letter}" in ctx)
    assert present == 2


def test_pretool_blocks_always_kept_even_with_many_hints(temp_db, monkeypatch):
    # 1 block + 3 hints all match `git status`. Cap=2 should keep the block
    # and exactly one hint.
    _seed_block(temp_db, "block-x", "Block body X for git", ["git", "status"])
    _seed_hint(temp_db, "hint-a", "Hint body A for git", ["git", "status"])
    _seed_hint(temp_db, "hint-b", "Hint body B for git", ["git", "status"])
    _seed_hint(temp_db, "hint-c", "Hint body C for git", ["git", "status"])

    payload = {
        "session_id": "sess-cap-2",
        "cwd": "/tmp/x",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "tu-cap-2",
    }
    result = _run_hook(pretool, payload, monkeypatch)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"  # block forces deny
    ctx = hso["additionalContext"]
    assert "Block body X" in ctx
    hints_in = sum(1 for letter in "ABC" if f"Hint body {letter}" in ctx)
    assert hints_in == 1


def test_pretool_env_override_raises_cap(temp_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_MAX_MEMORIES_PER_CALL", "5")
    for letter in "ABCD":
        _seed_hint(temp_db, f"hint-{letter}", f"Hint body {letter}", ["git", "status"])

    payload = {
        "session_id": "sess-cap-3",
        "cwd": "/tmp/x",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "tu-cap-3",
    }
    result = _run_hook(pretool, payload, monkeypatch)
    ctx = result["hookSpecificOutput"]["additionalContext"]
    present = sum(1 for letter in "ABCD" if f"Hint body {letter}" in ctx)
    assert present == 4  # all four fit under cap=5


def test_pretool_invalid_env_falls_back_to_default(temp_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_MAX_MEMORIES_PER_CALL", "not-a-number")
    for letter in "ABCD":
        _seed_hint(temp_db, f"hint-{letter}", f"Hint body {letter}", ["git", "status"])

    payload = {
        "session_id": "sess-cap-4",
        "cwd": "/tmp/x",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "tu-cap-4",
    }
    result = _run_hook(pretool, payload, monkeypatch)
    ctx = result["hookSpecificOutput"]["additionalContext"]
    present = sum(1 for letter in "ABCD" if f"Hint body {letter}" in ctx)
    assert present == 2  # fall back to default


def test_post_tool_failure_caps_hints(temp_db, monkeypatch):
    for letter in "ABCD":
        _seed_hint(temp_db, f"phf-hint-{letter}", f"PHF body {letter}", ["git", "status"])

    payload = {
        "session_id": "sess-cap-5",
        "cwd": "/tmp/x",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "tu-cap-5",
        "error": "Exit code 1",
        "is_interrupt": False,
    }
    result = _run_hook(post_tool_failure, payload, monkeypatch)
    ctx = result["hookSpecificOutput"]["additionalContext"]
    present = sum(1 for letter in "ABCD" if f"PHF body {letter}" in ctx)
    assert present == 2
