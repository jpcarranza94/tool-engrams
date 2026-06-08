"""post_tool's failure→success behavior.

post_tool does not credit a hint when a prior failed call's first_token
succeeds again — usefulness is judged by the evaluation watcher
(`engram judge`), not inferred from a retry succeeding. The failure→success
detection only fires an early watcher tick (the failure surface's evidence
window just closed), and leaves the counters and the surface outcome untouched.
"""

from __future__ import annotations

import io
import json
import sys
import time

import pytest

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


@pytest.fixture
def captured_ticks(monkeypatch):
    """Capture recovery-tick spawns (both roles) instead of launching detached
    processes. Returns dicts with the spawned role's tick `kind`."""
    calls: list[dict] = []
    monkeypatch.setattr(
        post_tool.tick, "trigger",
        lambda session_id, tpath, cwd, reason, flush=False: calls.append(
            {"kind": "formation", "reason": reason}
        ),
    )
    monkeypatch.setattr(
        post_tool.tick, "trigger_eval",
        lambda session_id, tpath, cwd, reason, flush=False: calls.append(
            {"kind": "eval", "reason": reason}
        ),
    )
    return calls


def _useful(conn, name: str) -> int:
    return conn.execute(
        "SELECT useful_count FROM memories WHERE name = ?", (name,)
    ).fetchone()["useful_count"]


def _failure_outcome(conn, session_id: str):
    return conn.execute(
        "SELECT outcome FROM session_surfaces "
        "WHERE session_id = ? AND hook = 'post_tool_use_failure'",
        (session_id,),
    ).fetchone()["outcome"]


def test_failure_then_success_does_not_credit_but_fires_recovery_tick(
    temp_db, monkeypatch, captured_ticks
):
    _seed_hint(
        temp_db, "git-force-push",
        "Without this memory, Claude would force push and overwrite teammates' commits.",
        ["git", "push", "--force"],
    )

    # 1. The failed `git push --force` surfaces the hint (outcome NULL).
    _run_hook(post_tool_failure, {
        "session_id": "sess-X",
        "cwd": "/tmp/x",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
        "tool_use_id": "tu-1",
        "error": "Exit code 1",
        "is_interrupt": False,
    }, monkeypatch)
    assert _useful(temp_db, "git-force-push") == 0
    assert _failure_outcome(temp_db, "sess-X") is None

    # 2. The retry `git push --force-with-lease` succeeds.
    _run_hook(post_tool, {
        "session_id": "sess-X",
        "cwd": "/tmp/x",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force-with-lease origin main"},
        "tool_use_id": "tu-2",
        "tool_response": "Everything up-to-date",
    }, monkeypatch)

    # Still NOT credited — the eval watcher judges heeding, not post_tool.
    assert _useful(temp_db, "git-force-push") == 0
    assert _failure_outcome(temp_db, "sess-X") is None
    # Recovery fires BOTH ticks: formation (episode complete) and eval (the
    # failure surface's evidence window just closed).
    assert {c["kind"] for c in captured_ticks} == {"formation", "eval"}
    assert all(c["reason"] == "recovery" for c in captured_ticks)


def test_no_recovery_tick_for_different_first_token(temp_db, monkeypatch, captured_ticks):
    _seed_hint(temp_db, "git-only", "Hint about git.", ["git", "push"])

    _run_hook(post_tool_failure, {
        "session_id": "sess-Y",
        "cwd": "/tmp/y",
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin main"},
        "tool_use_id": "tu-1",
        "error": "Exit code 1",
        "is_interrupt": False,
    }, monkeypatch)

    # Next call is `ls -la` — different first_token: no recovery, no credit.
    _run_hook(post_tool, {
        "session_id": "sess-Y",
        "cwd": "/tmp/y",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "tool_use_id": "tu-2",
        "tool_response": "drwxr-xr-x ...",
    }, monkeypatch)

    assert _useful(temp_db, "git-only") == 0
    assert captured_ticks == []


def test_pretool_surface_not_credited_on_success(temp_db, monkeypatch, captured_ticks):
    """A pre_tool_use surface is not credited as 'helpful' on a later success."""
    _seed_hint(temp_db, "ph", "psql replica hint", ["psql", "-h"])

    _run_hook(pretool, {
        "session_id": "sess-PT",
        "cwd": "/tmp/x",
        "tool_name": "Bash",
        "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
        "tool_use_id": "tu-1",
    }, monkeypatch)

    _run_hook(post_tool, {
        "session_id": "sess-PT",
        "cwd": "/tmp/x",
        "tool_name": "Bash",
        "tool_input": {"command": "psql -h replica.internal -c 'SELECT 1'"},
        "tool_use_id": "tu-1",
        "tool_response": "1 row",
    }, monkeypatch)

    assert _useful(temp_db, "ph") == 0
    surface = temp_db.execute(
        "SELECT outcome FROM session_surfaces "
        "WHERE session_id = 'sess-PT' AND hook = 'pre_tool_use'"
    ).fetchone()
    assert surface["outcome"] is None
