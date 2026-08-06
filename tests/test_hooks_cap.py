"""Per-call memory cap (ENGRAM_MAX_MEMORIES_PER_CALL).

Both pretool and post_tool_failure surface at most N memories per call
through the shared `_skip.rank_and_cap`. Default N=4. Blocks always
preserved at the pretool layer; hints trimmed by score order.
"""

from __future__ import annotations

import io
import json
import sys
import time

from toolengrams.hooks import post_tool_failure, pretool
from toolengrams.hooks._skip import DEFAULT_MAX_MEMORIES_PER_CALL, rank_and_cap
from toolengrams.models import Candidate
from toolengrams.reinforcement.scoring import final_score


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


def test_pretool_caps_hints_at_default(temp_db, monkeypatch):
    # 6 hints all match `git status`; only DEFAULT_MAX_MEMORIES_PER_CALL surface.
    for letter in "ABCDEF":
        _seed_hint(temp_db, f"hint-{letter}", f"Hint body {letter} about git",
                   ["git", "status"])

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
    present = sum(1 for letter in "ABCDEF" if f"Hint body {letter}" in ctx)
    assert present == DEFAULT_MAX_MEMORIES_PER_CALL


def test_pretool_blocks_always_kept_even_with_many_hints(temp_db, monkeypatch):
    # 1 block + 6 hints all match `git status`. The block is kept and the
    # hints fill the remaining cap-1 slots.
    _seed_block(temp_db, "block-x", "Block body X for git", ["git", "status"])
    for letter in "ABCDEF":
        _seed_hint(temp_db, f"hint-{letter}", f"Hint body {letter} for git",
                   ["git", "status"])

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
    hints_in = sum(1 for letter in "ABCDEF" if f"Hint body {letter}" in ctx)
    assert hints_in == DEFAULT_MAX_MEMORIES_PER_CALL - 1


def test_pretool_env_override_raises_cap(temp_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_MAX_MEMORIES_PER_CALL", "5")
    for letter in "ABCDEF":
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
    present = sum(1 for letter in "ABCDEF" if f"Hint body {letter}" in ctx)
    assert present == 5  # above the default


def test_pretool_invalid_env_falls_back_to_default(temp_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_MAX_MEMORIES_PER_CALL", "not-a-number")
    for letter in "ABCDEF":
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
    present = sum(1 for letter in "ABCDEF" if f"Hint body {letter}" in ctx)
    assert present == DEFAULT_MAX_MEMORIES_PER_CALL  # fall back to default


def test_post_tool_failure_caps_hints(temp_db, monkeypatch):
    for letter in "ABCDEF":
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
    present = sum(1 for letter in "ABCDEF" if f"PHF body {letter}" in ctx)
    assert present == DEFAULT_MAX_MEMORIES_PER_CALL


def test_post_tool_failure_under_cap_surfaces_all(temp_db, monkeypatch):
    """Under-cap case: all hints surface and their surface_counts bump correctly."""
    _seed_hint(temp_db, "phf-hint-X", "PHF body X", ["git", "status"])

    payload = {
        "session_id": "sess-cap-6",
        "cwd": "/tmp/x",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "tu-cap-6",
        "error": "Exit code 1",
        "is_interrupt": False,
    }
    result = _run_hook(post_tool_failure, payload, monkeypatch)
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "PHF body X" in ctx
    row = temp_db.execute(
        "SELECT surface_count FROM memories WHERE name = 'phf-hint-X'"
    ).fetchone()
    assert row["surface_count"] == 1


def _cand(memory_id: int, *, useful: int, noise: int, tokens: tuple[str, ...]) -> Candidate:
    c = Candidate(
        memory_id=memory_id, name=f"m{memory_id}", body="body",
        matched_tokens=tokens, matched_path=None, surface_count=0,
        useful_count=useful, noise_count=noise, last_surfaced_ts=0,
        pinned=False, kind="hint", scope="global",
    )
    c.final_score = final_score(c)
    return c


def test_proven_memory_is_not_starved_by_longer_unproven_triggers():
    """A proven memory (useful>=3, noise=0) must not lose its slot to unproven
    memories that merely carry longer triggers — the cap+sort defect that trimmed
    22% of real matches, victimising the highest-quality memories first."""
    proven = _cand(1, useful=6, noise=0, tokens=("ssh",))
    unproven = [
        _cand(i, useful=0, noise=0, tokens=("ssh", "-i", "key", f"host{i}"))
        for i in range(2, 2 + DEFAULT_MAX_MEMORIES_PER_CALL)
    ]
    kept = rank_and_cap(unproven + [proven])
    assert len(kept) == DEFAULT_MAX_MEMORIES_PER_CALL
    assert kept[0] is proven


def test_env_override_clamped_to_ceiling(temp_db, monkeypatch):
    """ENGRAM_MAX_MEMORIES_PER_CALL=200 should be clamped, not honored as-is."""
    monkeypatch.setenv("ENGRAM_MAX_MEMORIES_PER_CALL", "200")
    from toolengrams.hooks._skip import MAX_MEMORIES_PER_CALL_CEILING, max_memories_per_call

    assert max_memories_per_call() == MAX_MEMORIES_PER_CALL_CEILING
