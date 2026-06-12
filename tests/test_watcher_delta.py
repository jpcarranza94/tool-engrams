"""File-based delta hand-off: the watcher writes the transcript delta to
./delta.txt in the session sandbox (granting a scoped Read), and the claude -p
message carries the prompt + a pointer to that file — never the raw delta inline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from toolengrams.engine import EngineResult
from toolengrams.watcher import SessionResult, agent, log as wlog, tick

from .conftest import make_fake_engine


@pytest.fixture(autouse=True)
def _sandbox_root(tmp_path, monkeypatch):
    """The stable sandbox dirs persist across calls by design — root them under
    tmp_path so tests don't touch the real <engram home>/sandboxes."""
    monkeypatch.setattr(agent, "_sandbox_root", lambda: tmp_path)


def _bash_line(cmd: str) -> str:
    return json.dumps({
        "type": "message",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
        ]},
    }) + "\n"


def test_run_watcher_session_writes_delta_file_and_grants_scoped_read(monkeypatch):
    captured = {}

    def fake_invoke(req):
        cwd = Path(req.cwd)
        captured["message"] = req.prompt
        captured["delta"] = (cwd / "delta.txt").read_text()
        captured["settings"] = json.loads(
            (cwd / ".claude" / "settings.local.json").read_text())
        captured["env"] = req.env
        return EngineResult(ok=True, engine="claude-code")

    monkeypatch.setattr(agent, "get_engine", lambda: make_fake_engine(fake_invoke))

    r = agent.run_watcher_session(
        "formation", "PROMPT: read ./delta.txt",
        work_session_id="s1", run_id=5,
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
    # The engine-agnostic dispatch guard rides along in the child env.
    assert captured["env"]["ENGRAM_ALLOWED_VERBS"] == "remember"


def test_role_grants_do_not_grow_across_calls(monkeypatch):
    """Repeated sessions in one sandbox must regenerate identical grants —
    never accumulate (the old mutate-the-module-allowlist bug class)."""
    seen = []

    def fake_invoke(req):
        seen.append(json.loads(
            (Path(req.cwd) / ".claude" / "settings.local.json").read_text()))
        return EngineResult(ok=True, engine="claude-code")

    monkeypatch.setattr(agent, "get_engine", lambda: make_fake_engine(fake_invoke))

    agent.run_watcher_session("formation", "p",
                              work_session_id="s1", delta="a")
    agent.run_watcher_session("formation", "p",
                              work_session_id="s1", delta="b")

    assert seen[0] == seen[1]
    allow = seen[0]["permissions"]["allow"]
    assert allow.count("Bash(engram remember *)") == 1
    assert agent.ROLE_COMMAND_PREFIXES["formation"] == ("engram remember",)


def test_run_session_fail_open_on_delta_write_error(monkeypatch):
    """A sandbox-setup failure (e.g. the delta write) must not raise into the
    tick — it returns ok=False with a reason so the run row finalizes as error."""
    import pathlib
    monkeypatch.setattr(agent, "get_engine", lambda: make_fake_engine())
    monkeypatch.setattr(pathlib.Path, "write_text",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    r = agent.run_watcher_session("formation", "p",
                                  work_session_id="s1", delta="x")
    assert r.ok is False

    assert "setup failed" in (r.error or "")


def test_sandbox_cwd_is_stable_per_session_and_role(monkeypatch):
    """The sandbox dir must
    be the SAME across ticks of one (work session, role) — and different across
    sessions and roles (stable slug for the recursion guard + cleanup)."""
    cwds = []
    monkeypatch.setattr(agent, "get_engine", lambda: make_fake_engine(
        lambda req: (cwds.append(req.cwd),
                     EngineResult(ok=True, engine="claude-code"))[1]))

    agent.run_watcher_session("formation", "p",
                              work_session_id="sess-a", delta="a")
    agent.run_watcher_session("formation", "p",
                              work_session_id="sess-a", delta="b")
    agent.run_watcher_session("formation", "p",
                              work_session_id="sess-b", delta="c")
    agent.run_watcher_session("eval", "p",
                              work_session_id="sess-a", delta="d")

    assert cwds[0] == cwds[1]                      # same session+role → same cwd
    assert cwds[0] != cwds[2]                      # other session → other cwd
    assert cwds[0] != cwds[3]                      # other role → other cwd
    # Recursion guard + consolidation filter both key off this basename prefix.
    assert Path(cwds[0]).name == "engram-formation-sess-a"
    # The delta file is overwritten in place, not accumulated.
    assert (Path(cwds[0]) / "delta.txt").read_text() == "b"


def test_sandbox_id_is_sanitized_against_traversal(tmp_path, monkeypatch):
    """A hostile or malformed session id must not escape the sandbox root."""
    cwds = []
    monkeypatch.setattr(agent, "get_engine", lambda: make_fake_engine(
        lambda req: (cwds.append(req.cwd),
                     EngineResult(ok=True, engine="claude-code"))[1]))

    r = agent.run_watcher_session("formation", "p",
                                  work_session_id="../../../etc/evil", delta="x")

    assert r.ok
    cwd = Path(cwds[0])
    assert cwd.parent == tmp_path                  # still under the root
    assert ".." not in cwd.name and "/" not in cwd.name


def test_sandbox_created_user_only_and_symlink_rejected(tmp_path, monkeypatch):
    """The sandbox holds transcript excerpts and the settings.local.json
    permission boundary: 0700 on create, and a pre-existing symlink (squat /
    swap) is refused rather than silently used."""
    monkeypatch.setattr(agent, "get_engine", lambda: make_fake_engine())

    r = agent.run_watcher_session("formation", "p",
                                  work_session_id="sess-perm", delta="x")
    assert r.ok
    mode = (tmp_path / "engram-formation-sess-perm").stat().st_mode & 0o777
    assert mode == 0o700

    victim = tmp_path / "victim"
    victim.mkdir()
    (tmp_path / "engram-eval-sess-link").symlink_to(victim)
    r = agent.run_watcher_session("eval", "p",
                                  work_session_id="sess-link", delta="x")
    assert r.ok is False
    assert "not a directory we own" in (r.error or "")



def test_tick_routes_activity_through_delta_not_inline(temp_db, tmp_path, monkeypatch):
    # A command that does NOT appear in the prompt's own examples, so we can tell
    # the delta apart from the prompt text.
    cmd = "zqxfrobnicate --wibble /tmp/quux"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line(cmd))
    seen = {}

    def runner(role, message, run_id=None, delta="", **kw):
        seen["message"] = message
        seen["delta"] = delta
        return SessionResult(ok=True)

    monkeypatch.setattr(tick, "engine_available", lambda: True)
    monkeypatch.setattr(tick, "log_path", lambda: tmp_path / "watcher.log")
    monkeypatch.setattr(wlog, "log_path", lambda: tmp_path / "watcher.log")
    monkeypatch.setattr(tick, "run_watcher_session", runner)
    tick.ensure_row("s", str(transcript), "/cwd")

    tick.run_tick("s", str(transcript), "/cwd")

    assert "zqxfrobnicate" in seen["delta"]          # activity → delta (→ file)
    assert "zqxfrobnicate" not in seen["message"]    # not inlined in the prompt
    assert "delta.txt" in seen["message"]            # prompt points at the file
