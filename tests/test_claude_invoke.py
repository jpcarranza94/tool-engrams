"""Tests for the unified claude -p invocation seam (claude_invoke.py)."""

from __future__ import annotations

import json
import subprocess

from toolengrams import claude_invoke


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_nonzero_exit_surfaces_stderr_tail(monkeypatch):
    monkeypatch.setattr(claude_invoke.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout="", returncode=1,
                                                 stderr="Error: overloaded_error (529)\n"))
    r = claude_invoke.invoke_claude_agent("p", timeout=5, claude_bin="/c")
    assert r.returncode == 1
    assert r.error is not None
    assert "exit 1" in r.error and "529" in r.error
    assert "\n" not in r.error          # newlines flattened for the run-log / dashboard


def test_zero_exit_has_no_error(monkeypatch):
    monkeypatch.setattr(claude_invoke.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout='{"result":"ok"}', returncode=0,
                                                 stderr="some warning"))
    r = claude_invoke.invoke_claude_agent("p", timeout=5, claude_bin="/c")
    assert r.error is None


# ---------- invoke_claude_agent: argv construction ----------


def test_invoke_builds_argv_with_all_flags(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return _Proc(stdout='{"result":"ok"}', returncode=0)

    monkeypatch.setattr(claude_invoke.subprocess, "run", fake_run)
    r = claude_invoke.invoke_claude_agent(
        "hi", timeout=30, model="opus", schema="{}", resume="sid",
        bare=True, cwd="/tmp", env={"X": "1"}, claude_bin="/bin/claude",
    )

    argv = captured["argv"]
    assert argv[:2] == ["/bin/claude", "-p"]
    assert "--bare" in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--json-schema") + 1] == "{}"
    assert argv[argv.index("--resume") + 1] == "sid"
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert argv[-2:] == ["--", "hi"]          # prompt is last, after the -- guard
    assert captured["kw"]["cwd"] == "/tmp"
    assert captured["kw"]["env"] == {"X": "1"}
    assert captured["kw"]["timeout"] == 30
    assert r.stdout == '{"result":"ok"}' and r.returncode == 0
    assert r.error is None and r.timed_out is False


def test_invoke_omits_unset_optional_flags(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude_invoke.subprocess, "run",
                        lambda argv, **kw: captured.update(argv=argv) or _Proc())
    claude_invoke.invoke_claude_agent("p", timeout=10, claude_bin="/c")
    argv = captured["argv"]
    for flag in ("--bare", "--model", "--json-schema", "--resume"):
        assert flag not in argv
    assert "--output-format" in argv          # always present


# ---------- invoke_claude_agent: failure modes (never raises) ----------


def test_invoke_timeout_returns_flag(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw["timeout"])

    monkeypatch.setattr(claude_invoke.subprocess, "run", boom)
    r = claude_invoke.invoke_claude_agent("p", timeout=5, claude_bin="/c")
    assert r.timed_out is True and r.returncode == 1 and r.stdout == ""
    assert "timed out" in r.error


def test_invoke_spawn_error_returns_flag(monkeypatch):
    def boom(*a, **k):
        raise OSError("exec failed")

    monkeypatch.setattr(claude_invoke.subprocess, "run", boom)
    r = claude_invoke.invoke_claude_agent("p", timeout=5, claude_bin="/c")
    assert r.timed_out is False and r.returncode == 1 and r.error.startswith("failed to spawn")


def test_invoke_missing_binary(monkeypatch):
    monkeypatch.setattr(claude_invoke.shutil, "which", lambda name: None)
    r = claude_invoke.invoke_claude_agent("p", timeout=5)  # no claude_bin → resolve fails
    assert r.error == "claude CLI not found on PATH" and r.returncode == 1


# ---------- parse_claude_json_output ----------


def test_parse_prefers_structured_output():
    out = json.dumps({"structured_output": {"action": "none"}, "session_id": "w"})
    assert json.loads(claude_invoke.parse_claude_json_output(out)) == {"action": "none"}


def test_parse_falls_back_to_result():
    assert claude_invoke.parse_claude_json_output(json.dumps({"result": "hello"})) == "hello"


def test_parse_garbage_returns_empty():
    assert claude_invoke.parse_claude_json_output("not json at all") == ""


# ---------- write_agent_settings ----------


def test_write_agent_settings(tmp_path):
    claude_invoke.write_agent_settings(tmp_path, ["Read", "Bash(engram *)"])
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert data["permissions"]["allow"] == ["Read", "Bash(engram *)"]
