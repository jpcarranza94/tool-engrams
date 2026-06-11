"""Same-session suppression (ADR-0006): a hint never surfaces into the session
that formed it; blocks are exempt; manual saves (NULL origin) never suppressed."""

from __future__ import annotations

import io
import json
import sys
import time

from toolengrams import memory_store
from toolengrams.cli import remember
from toolengrams.hooks import pretool


def _run_pretool(payload: dict, monkeypatch) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    assert pretool.main() == 0
    out = buf.getvalue().strip()
    return json.loads(out) if out else {}


def _payload(session_id: str, command: str) -> dict:
    return {
        "session_id": session_id,
        "cwd": "/tmp/test-projects/foo",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": "tu-1",
    }


def _save(monkeypatch, body: str, *, kind="hint", origin: str | None = None,
          trigger: str) -> None:
    if origin is None:
        monkeypatch.delenv("ENGRAM_ORIGIN_SESSION", raising=False)
        args = [body, "--kind", kind, "--scope", "global", "--trigger", trigger]
    else:
        args = [body, "--kind", kind, "--scope", "global", "--trigger", trigger,
                "--origin-session", origin]
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    assert remember.main(args) == 0
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)


def test_hint_suppressed_in_origin_session(temp_db, monkeypatch):
    _save(monkeypatch, "kubectl ctx hint body", origin="sess-origin",
          trigger="kubectl get")
    result = _run_pretool(_payload("sess-origin", "kubectl get pods"), monkeypatch)
    assert result == {}  # suppressed — same session that formed it


def test_hint_surfaces_in_other_sessions(temp_db, monkeypatch):
    _save(monkeypatch, "kubectl ctx hint body", origin="sess-origin",
          trigger="kubectl get")
    result = _run_pretool(_payload("sess-OTHER", "kubectl get pods"), monkeypatch)
    assert "kubectl ctx hint body" in result["hookSpecificOutput"]["additionalContext"]


def test_block_exempt_from_same_session_suppression(temp_db, monkeypatch):
    _save(monkeypatch, "never force push body", kind="block",
          origin="sess-origin", trigger="git push --force")
    result = _run_pretool(_payload("sess-origin", "git push --force origin main"),
                          monkeypatch)
    hso = result["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"  # enforcement fires at home


def test_manual_save_never_suppressed(temp_db, monkeypatch):
    _save(monkeypatch, "manual save hint body", trigger="terraform apply")
    result = _run_pretool(_payload("any-session", "terraform apply -auto-approve"),
                          monkeypatch)
    assert "manual save hint body" in result["hookSpecificOutput"]["additionalContext"]


def test_env_fallback_sets_origin(temp_db, monkeypatch):
    monkeypatch.setenv("ENGRAM_ORIGIN_SESSION", "sess-env")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    assert remember.main(["env origin body", "--kind", "hint", "--scope", "global",
                          "--trigger", "helm upgrade"]) == 0
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    mid = json.loads(buf.getvalue())["memory"]["id"]
    assert memory_store.get(temp_db, mid).origin_session_id == "sess-env"


def test_dedup_update_echoes_previous_body(temp_db, monkeypatch):
    _save(monkeypatch, "old guidance: use --foo with `mycli deploy`",
          trigger="mycli deploy")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    assert remember.main(["new guidance: use --bar with `mycli deploy`",
                          "--kind", "hint", "--scope", "global",
                          "--trigger", "mycli deploy"]) == 0
    out = json.loads(buf.getvalue())
    assert out["action"] == "updated"
    assert "old guidance" in out["existing_match"]["previous_body"]
    assert "merged body" in out["existing_match"]["merge_note"]


def test_failure_hook_also_suppresses_origin_session(temp_db, monkeypatch):
    """PostToolUseFailure shares the ADR-0006 filter (hints only by retrieve-time
    invariant — pin it so relaxing the kind filter doesn't reopen the loop)."""
    from toolengrams.hooks import post_tool_failure

    _save(monkeypatch, "failure-path hint body", origin="sess-origin",
          trigger="flaky-tool run")
    payload = {
        "session_id": "sess-origin",
        "cwd": "/tmp/test-projects/foo",
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "flaky-tool run --now"},
        "tool_use_id": "tu-f",
        "error": "Exit code 1",
        "is_interrupt": False,
        "transcript_path": "",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    assert post_tool_failure.main() == 0
    out = buf.getvalue().strip()
    assert (json.loads(out) if out else {}) == {}  # suppressed at home

    payload["session_id"] = "sess-OTHER"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    assert post_tool_failure.main() == 0
    assert "failure-path hint body" in buf.getvalue()  # surfaces elsewhere


def test_dedup_update_restamps_origin_to_updating_session(temp_db, monkeypatch):
    """ADR-0006: a body fully replaced by session B's watcher belongs to B —
    B's echo is now the one to suppress."""
    from toolengrams import memory_store

    _save(monkeypatch, "v1 guidance for `mycli deploy`", origin="sess-A",
          trigger="mycli deploy")
    _save(monkeypatch, "v2 guidance for `mycli deploy` rewritten", origin="sess-B",
          trigger="mycli deploy")

    rows = temp_db.execute(
        "SELECT id, origin_session_id FROM memories").fetchall()
    assert len(rows) == 1                      # deduped into one row
    assert rows[0]["origin_session_id"] == "sess-B"  # re-stamped to the updater
