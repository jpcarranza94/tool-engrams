"""Unit tests for the SessionStart handler — formation guidance injection."""

from __future__ import annotations

import io
import json
import sys

from toolengrams.commands import session_start


def _run(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    rc = session_start.main()
    assert rc == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def test_session_start_emits_guidance(monkeypatch):
    result = _run(
        {"session_id": "s1", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
        monkeypatch,
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "ToolEngrams: tool-bound memory" in ctx
    assert "engram remember" in ctx


def test_guidance_explains_rejection(monkeypatch):
    result = _run(
        {"session_id": "s2", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
        monkeypatch,
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "backticked commands" in ctx
    assert "rejected" in ctx


def test_guidance_separates_from_builtin_memory(monkeypatch):
    result = _run(
        {"session_id": "s3", "cwd": "/tmp/foo", "hook_event_name": "SessionStart", "source": "startup"},
        monkeypatch,
    )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "built-in memory" in ctx.lower() or "Claude's built-in" in ctx
