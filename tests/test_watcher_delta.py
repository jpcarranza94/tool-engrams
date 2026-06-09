"""File-based delta hand-off: the watcher writes the transcript delta to
./delta.txt in the session sandbox (granting a scoped Read), and the claude -p
message carries the prompt + a pointer to that file — never the raw delta inline.
"""

from __future__ import annotations

import json
from pathlib import Path

from toolengrams.claude_invoke import ClaudeResult
from toolengrams.watcher import SessionResult, agent, log as wlog, tick


def _bash_line(cmd: str) -> str:
    return json.dumps({
        "type": "message",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
        ]},
    }) + "\n"


def test_run_watcher_session_writes_delta_file_and_grants_scoped_read(monkeypatch):
    captured = {}

    def fake_invoke(message, **kw):
        cwd = Path(kw["cwd"])
        captured["message"] = message
        captured["delta"] = (cwd / "delta.txt").read_text()
        captured["settings"] = json.loads(
            (cwd / ".claude" / "settings.local.json").read_text())
        return ClaudeResult(stdout='{"session_id": "w1", "result": "ok"}', returncode=0)

    monkeypatch.setattr(agent, "invoke_claude_agent", fake_invoke)
    monkeypatch.setattr(agent, "CLAUDE_BIN", "/usr/bin/claude")

    r = agent.run_watcher_session(
        "formation", "PROMPT: read ./delta.txt", resume=None, run_id=5,
        delta="USER: hi\nTOOL (Bash): git push --force origin main",
    )
    assert r.ok
    # The activity is in the FILE, not the message argv.
    assert "git push --force" in captured["delta"]
    assert "git push --force" not in captured["message"]
    # The allowlist keeps the role verb AND a Read scoped to the delta file.
    allow = captured["settings"]["permissions"]["allow"]
    assert "Bash(engram remember *)" in allow
    assert any(a.startswith("Read(") and "delta.txt" in a for a in allow)


def test_run_session_does_not_mutate_role_allowlist(monkeypatch):
    """`allow + [Read(...)]` must build a new list — never grow the module-level
    ROLE_ALLOWLIST across calls."""
    monkeypatch.setattr(agent, "invoke_claude_agent",
                        lambda message, **kw: ClaudeResult(stdout='{"session_id": "w"}',
                                                           returncode=0))
    monkeypatch.setattr(agent, "CLAUDE_BIN", "/usr/bin/claude")
    before = list(agent.ROLE_ALLOWLIST["formation"])

    agent.run_watcher_session("formation", "p", resume=None, delta="a")
    agent.run_watcher_session("formation", "p", resume=None, delta="b")

    assert agent.ROLE_ALLOWLIST["formation"] == before   # unchanged after two calls
    assert len(agent.ROLE_ALLOWLIST["formation"]) == 1


def test_run_session_fail_open_on_delta_write_error(monkeypatch):
    """A sandbox-setup failure (e.g. the delta write) must not raise into the
    tick — it returns ok=False with a reason so the run row finalizes as error."""
    import pathlib
    monkeypatch.setattr(agent, "CLAUDE_BIN", "/usr/bin/claude")
    monkeypatch.setattr(pathlib.Path, "write_text",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    r = agent.run_watcher_session("formation", "p", resume="sid", delta="x")
    assert r.ok is False
    assert r.watcher_session_id == "sid"        # resume id preserved for retry
    assert "setup failed" in (r.error or "")


def test_tick_routes_activity_through_delta_not_inline(temp_db, tmp_path, monkeypatch):
    # A command that does NOT appear in the prompt's own examples, so we can tell
    # the delta apart from the prompt text.
    cmd = "zqxfrobnicate --wibble /tmp/quux"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line(cmd))
    seen = {}

    def runner(role, message, resume, run_id=None, delta="", **kw):
        seen["message"] = message
        seen["delta"] = delta
        return SessionResult(ok=True, watcher_session_id="w1")

    monkeypatch.setattr(tick, "CLAUDE_BIN", "claude")
    monkeypatch.setattr(tick, "LOG_PATH", tmp_path / "watcher.log")
    monkeypatch.setattr(wlog, "LOG_PATH", tmp_path / "watcher.log")
    monkeypatch.setattr(tick, "run_watcher_session", runner)
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")

    assert "zqxfrobnicate" in seen["delta"]          # activity → delta (→ file)
    assert "zqxfrobnicate" not in seen["message"]    # not inlined in the prompt
    assert "delta.txt" in seen["message"]            # prompt points at the file
