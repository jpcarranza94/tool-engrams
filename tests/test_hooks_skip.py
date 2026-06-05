"""Unit tests for the shared hook skip helper + its two callers.

SessionStart now tracks a session (tick.ensure_row); UserPromptSubmit fires a
watcher tick only on a likely correction. Both still skip internal cwds and
watcher-child processes.
"""

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


# ---------- session_start: tracks real sessions, skips internal/child ----------


def _run_session_start(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = session_start.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def test_session_start_skips_consolidate_temp_cwd(monkeypatch):
    with patch.object(session_start.tick, "ensure_row") as mock:
        _run_session_start(
            {"session_id": "s-consolidate",
             "cwd": "/private/var/folders/q7/abc/T/engram-consolidate-xyz",
             "source": "startup"},
            monkeypatch,
        )
    mock.assert_not_called()


def test_session_start_tracks_real_user_cwd(monkeypatch):
    with patch.object(session_start.tick, "ensure_row") as mock, \
         patch.object(session_start.tick, "sweep_idle_sessions"):
        _run_session_start(
            {"session_id": "s-real", "cwd": "/Users/jpcar/projects/my-app", "source": "startup"},
            monkeypatch,
        )
    mock.assert_called_once()


def test_session_start_skips_when_watcher_child(monkeypatch):
    """A watcher-launched `claude` must not register/trigger watchers."""
    monkeypatch.setenv("ENGRAM_IN_WATCHER", "1")
    with patch.object(session_start.tick, "ensure_row") as mock:
        _run_session_start(
            {"session_id": "s-wc", "cwd": "/Users/jpcar/projects/my-app", "source": "startup"},
            monkeypatch,
        )
    mock.assert_not_called()


# ---------- user_prompt: tick on correction, otherwise just track ----------


def _run_user_prompt(payload: dict, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return user_prompt.main()


def test_user_prompt_triggers_tick_on_correction(monkeypatch):
    with patch.object(user_prompt.tick, "ensure_row"), \
         patch.object(user_prompt.tick, "trigger") as trig:
        _run_user_prompt(
            {"session_id": "s1", "cwd": "/Users/jpcar/projects/x",
             "transcript_path": "/t.jsonl",
             "prompt": "no, use --force-with-lease instead"},
            monkeypatch,
        )
    trig.assert_called_once()


def test_user_prompt_no_tick_on_normal_prompt(monkeypatch):
    with patch.object(user_prompt.tick, "ensure_row") as ens, \
         patch.object(user_prompt.tick, "trigger") as trig:
        _run_user_prompt(
            {"session_id": "s1", "cwd": "/Users/jpcar/projects/x",
             "transcript_path": "/t.jsonl",
             "prompt": "please add a feature to the dashboard"},
            monkeypatch,
        )
    ens.assert_called_once()   # session still tracked
    trig.assert_not_called()   # but no tick — Stop will handle normal formation


def test_user_prompt_skips_internal_cwd(monkeypatch):
    with patch.object(user_prompt.tick, "ensure_row") as ens, \
         patch.object(user_prompt.tick, "trigger") as trig:
        _run_user_prompt(
            {"session_id": "s1", "cwd": "/tmp/engram-consolidate-xyz",
             "prompt": "no, that's wrong"},
            monkeypatch,
        )
    ens.assert_not_called()
    trig.assert_not_called()


def test_user_prompt_skips_when_watcher_child(monkeypatch):
    monkeypatch.setenv("ENGRAM_IN_WATCHER", "1")
    with patch.object(user_prompt.tick, "ensure_row") as ens, \
         patch.object(user_prompt.tick, "trigger") as trig:
        _run_user_prompt(
            {"session_id": "s1", "cwd": "/Users/jpcar/projects/x",
             "prompt": "no, that's wrong"},
            monkeypatch,
        )
    ens.assert_not_called()
    trig.assert_not_called()
