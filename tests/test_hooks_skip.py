"""Unit tests for the shared hook skip helper + its two callers."""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import patch

from toolengrams.hooks import session_start, user_prompt
from toolengrams.hooks._skip import is_internal_cwd


# ---------- is_internal_cwd ----------


def test_skip_consolidate_cwd():
    assert is_internal_cwd("/private/var/folders/q7/.../T/engram-consolidate-a274alix")


def test_skip_observe_cwd():
    assert is_internal_cwd("/tmp/engram-observe-abc123")


def test_skip_experiment_cwd():
    assert is_internal_cwd("/tmp/engram-experiment-foo")


def test_do_not_skip_user_project_cwd():
    assert not is_internal_cwd("/Users/jpcar/projects/srv-ergeon")
    assert not is_internal_cwd("/Users/jpcar/personal-projects/tool-engrams")
    assert not is_internal_cwd("/private/tmp/ssm-fix")


def test_do_not_skip_empty_cwd():
    assert not is_internal_cwd("")


def test_do_not_skip_trailing_slash_is_handled():
    assert is_internal_cwd("/tmp/engram-consolidate-xyz/")


# ---------- session_start respects the skip ----------


def _run_session_start(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = session_start.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def test_session_start_skips_consolidate_temp_cwd(monkeypatch):
    with patch.object(session_start, "spawn_watcher") as mock:
        _run_session_start(
            {
                "session_id": "s-consolidate",
                "cwd": "/private/var/folders/q7/abc/T/engram-consolidate-xyz",
                "source": "startup",
            },
            monkeypatch,
        )
    mock.assert_not_called()


def test_session_start_still_spawns_for_real_user_cwd(monkeypatch):
    with patch.object(session_start, "spawn_watcher") as mock:
        _run_session_start(
            {
                "session_id": "s-real",
                "cwd": "/Users/jpcar/projects/my-app",
                "source": "startup",
            },
            monkeypatch,
        )
    mock.assert_called_once()


# ---------- user_prompt respects the skip ----------


def _run_user_prompt(payload: dict, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return user_prompt.main()


def test_user_prompt_skips_consolidate_temp_cwd(temp_db, monkeypatch):
    with patch.object(user_prompt, "spawn_watcher") as mock:
        _run_user_prompt(
            {
                "session_id": "s-consolidate",
                "cwd": "/tmp/engram-consolidate-xyz",
            },
            monkeypatch,
        )
    mock.assert_not_called()


def test_user_prompt_still_spawns_for_real_user_cwd_when_no_watcher(temp_db, monkeypatch):
    with patch.object(user_prompt, "spawn_watcher") as mock, \
         patch.object(user_prompt, "is_pid_alive", return_value=False):
        _run_user_prompt(
            {
                "session_id": "s-real",
                "cwd": "/Users/jpcar/projects/my-app",
            },
            monkeypatch,
        )
    mock.assert_called_once()


def test_user_prompt_respawns_when_watcher_pid_alive_but_stale(
    temp_db, monkeypatch,
):
    """A wedged watcher: PID is alive but it hasn't ticked in a long time.
    The liveness check must catch this and respawn — otherwise the user
    silently goes without memory formation for the rest of the session.
    """
    import time as _time
    from toolengrams import db as _db

    # Insert a stale watcher_state row: PID will report alive (we mock that),
    # but last_checked_ts is older than 2× WATCHER_INTERVAL.
    long_ago = int(_time.time()) - (user_prompt.WATCHER_INTERVAL * 3)
    with _db.session() as conn:
        conn.execute(
            "INSERT INTO watcher_state "
            "(work_session_id, watcher_pid, transcript_path, "
            " last_line_read, last_checked_ts, cwd, created_ts) "
            "VALUES (?, 12345, '/tmp/x.jsonl', 0, ?, '/cwd', ?)",
            ("s-zombie", long_ago, long_ago),
        )

    with patch.object(user_prompt, "spawn_watcher") as mock, \
         patch.object(user_prompt, "is_pid_alive", return_value=True):
        _run_user_prompt(
            {"session_id": "s-zombie", "cwd": "/Users/jpcar/projects/x"},
            monkeypatch,
        )
    mock.assert_called_once()


def test_user_prompt_keeps_watcher_when_pid_alive_and_recent(
    temp_db, monkeypatch,
):
    """Healthy watcher: PID alive, last_checked_ts recent — no respawn."""
    import time as _time
    from toolengrams import db as _db

    recent = int(_time.time()) - 10  # well within grace
    with _db.session() as conn:
        conn.execute(
            "INSERT INTO watcher_state "
            "(work_session_id, watcher_pid, transcript_path, "
            " last_line_read, last_checked_ts, cwd, created_ts) "
            "VALUES (?, 12345, '/tmp/x.jsonl', 0, ?, '/cwd', ?)",
            ("s-healthy", recent, recent),
        )

    with patch.object(user_prompt, "spawn_watcher") as mock, \
         patch.object(user_prompt, "is_pid_alive", return_value=True):
        _run_user_prompt(
            {"session_id": "s-healthy", "cwd": "/Users/jpcar/projects/x"},
            monkeypatch,
        )
    mock.assert_not_called()
