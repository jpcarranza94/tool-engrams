"""The recursion guard: a `claude` running INSIDE a watcher session (marked by
$ENGRAM_IN_WATCHER) must not trigger any engram hook side effects when it runs
its own `engram remember` / `engram judge` Bash calls. Each hook main early-
returns `{}` and writes nothing.

This replaces what `--bare` used to provide for the formation watcher: v10
watcher sessions are permissioned (non-bare), so the guard is now the env flag
checked at the top of every hook (plus the internal-cwd backstop).
"""

from __future__ import annotations

import io
import json
import time

import pytest

from toolengrams.hooks import post_tool, post_tool_failure, pretool
from toolengrams.utils import WATCHER_CHILD_ENV


@pytest.fixture
def in_watcher(monkeypatch):
    monkeypatch.setenv(WATCHER_CHILD_ENV, "1")


def _seed_block(conn, tokens=("git", "push", "--force")):
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, created_ts) "
        "VALUES ('blk', '', 'b', 'block', 'global', NULL, ?)",
        (int(time.time()),),
    )
    mid = cur.lastrowid
    conn.execute(
        "INSERT INTO triggers (memory_id, kind, first_token, tokens_json) "
        "VALUES (?, 'token_subseq', ?, ?)",
        (mid, tokens[0], json.dumps(list(tokens))),
    )
    return mid


def _run(hook_module, payload, monkeypatch) -> dict:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    assert hook_module.main() == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def test_pretool_noop_in_watcher_child(temp_db, in_watcher, monkeypatch):
    """A block that would normally DENY emits nothing inside a watcher child."""
    _seed_block(temp_db)
    out = _run(pretool, {
        "session_id": "wc", "cwd": "/repo", "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
        "tool_use_id": "tu-1",
    }, monkeypatch)

    assert out == {}                              # no deny, no context injected
    n = temp_db.execute("SELECT COUNT(*) c FROM session_surfaces").fetchone()["c"]
    assert n == 0                                 # nothing surfaced/logged


def test_post_tool_noop_in_watcher_child(temp_db, in_watcher, monkeypatch):
    """No turn counted (and no recovery tick) for a watcher child's own call."""
    out = _run(post_tool, {
        "session_id": "wc", "tool_use_id": "tu-1", "tool_name": "Bash",
        "tool_response": "ok", "is_error": False,
    }, monkeypatch)

    assert out == {}
    n = temp_db.execute("SELECT COUNT(*) c FROM session_turns").fetchone()["c"]
    assert n == 0                                 # turn counter untouched


def test_post_tool_failure_noop_in_watcher_child(temp_db, in_watcher, monkeypatch):
    """No hint surfaced and the session is NOT armed for a watcher child."""
    _seed_block(temp_db, tokens=("git", "push"))  # any matching memory
    out = _run(post_tool_failure, {
        "session_id": "wc", "cwd": "/repo", "tool_name": "Bash",
        "tool_input": {"command": "git push origin main"},
        "tool_use_id": "tu-1", "error": "Exit code 1", "is_interrupt": False,
    }, monkeypatch)

    assert out == {}
    surfaces = temp_db.execute("SELECT COUNT(*) c FROM session_surfaces").fetchone()["c"]
    assert surfaces == 0
    # The watcher_state row should not have been created/armed by this call.
    armed = temp_db.execute(
        "SELECT armed FROM watcher_state WHERE work_session_id = 'wc' AND role = 'formation'"
    ).fetchone()
    assert armed is None                          # no arm side effect at all
