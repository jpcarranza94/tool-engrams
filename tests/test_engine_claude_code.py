"""Tests for the claude-code engine adapter (engine/claude_code.py)."""

from __future__ import annotations

import json
import subprocess

from toolengrams.engine import EngineRequest, SandboxSpec
from toolengrams.engine import claude_code

ENVELOPE = json.dumps({
    "session_id": "w1",
    "result": "ok",
    "total_cost_usd": 0.0231,
    "usage": {
        "input_tokens": 1200,
        "output_tokens": 350,
        "cache_read_input_tokens": 9000,
        "cache_creation_input_tokens": 400,
    },
})


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _which_ok(monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: "/bin/claude")


# ---------- invoke: argv construction ----------


def test_invoke_builds_argv_with_all_flags(monkeypatch):
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return _Proc(stdout='{"result":"ok"}', returncode=0)

    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run", fake_run)
    r = claude_code.invoke(EngineRequest(
        prompt="hi", timeout=30, model="opus", schema="{}",
        cwd="/tmp", env={"X": "1"},
    ))

    argv = captured["argv"]
    assert argv[:2] == ["/bin/claude", "-p"]
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--json-schema") + 1] == "{}"
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert argv[-2:] == ["--", "hi"]          # prompt is last, after the -- guard
    assert captured["kw"]["cwd"] == "/tmp"
    assert captured["kw"]["env"] == {"X": "1"}
    assert captured["kw"]["timeout"] == 30
    # Headless runner must never inherit a non-TTY stdin pipe (detached tick).
    assert captured["kw"]["stdin"] is subprocess.DEVNULL
    assert r.ok and r.stdout == '{"result":"ok"}' and r.returncode == 0
    assert r.error is None and r.timed_out is False
    assert r.engine == "claude-code"


def test_invoke_resolves_model_from_role(monkeypatch):
    captured = {}
    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda argv, **kw: captured.update(argv=argv) or _Proc())
    monkeypatch.setenv("ENGRAM_EVAL_MODEL", "haiku")

    claude_code.invoke(EngineRequest(prompt="p", timeout=10, role="eval"))
    argv = captured["argv"]
    assert argv[argv.index("--model") + 1] == "haiku"


def test_invoke_consolidation_role_omits_model(monkeypatch):
    captured = {}
    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda argv, **kw: captured.update(argv=argv) or _Proc())

    claude_code.invoke(EngineRequest(prompt="p", timeout=10, role="consolidation"))
    assert "--model" not in captured["argv"]
    assert "--json-schema" not in captured["argv"]


# ---------- invoke: failure modes (never raises) ----------


def test_invoke_timeout_returns_flag(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw["timeout"])

    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run", boom)
    r = claude_code.invoke(EngineRequest(prompt="p", timeout=5))
    assert r.ok is False and r.timed_out is True and r.returncode == 1
    assert "timed out" in r.error


def test_invoke_spawn_error_returns_flag(monkeypatch):
    def boom(*a, **k):
        raise OSError("exec failed")

    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run", boom)
    r = claude_code.invoke(EngineRequest(prompt="p", timeout=5))
    assert r.ok is False and r.timed_out is False and r.returncode == 1
    assert r.error.startswith("failed to spawn")


def test_invoke_missing_binary(monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: None)
    r = claude_code.invoke(EngineRequest(prompt="p", timeout=5))
    assert r.ok is False and r.error == "claude CLI not found on PATH"
    assert claude_code.is_available() is False


def test_nonzero_exit_surfaces_stderr_tail(monkeypatch):
    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout="", returncode=1,
                                                 stderr="Error: overloaded_error (529)\n"))
    r = claude_code.invoke(EngineRequest(prompt="p", timeout=5))
    assert r.ok is False and r.returncode == 1
    assert "exit 1" in r.error and "529" in r.error
    assert "\n" not in r.error          # newlines flattened for the run-log / dashboard


def test_zero_exit_has_no_error(monkeypatch):
    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout='{"result":"ok"}', returncode=0,
                                                 stderr="some warning"))
    r = claude_code.invoke(EngineRequest(prompt="p", timeout=5))
    assert r.ok and r.error is None


# ---------- result parsing: text + usage ----------


def test_text_prefers_structured_output(monkeypatch):
    out = json.dumps({"structured_output": {"action": "none"}, "session_id": "w"})
    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout=out, returncode=0))
    r = claude_code.invoke(EngineRequest(prompt="p", timeout=5))
    assert json.loads(r.text) == {"action": "none"}


def test_text_falls_back_to_result(monkeypatch):
    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout=json.dumps({"result": "hello"})))
    assert claude_code.invoke(EngineRequest(prompt="p", timeout=5)).text == "hello"


def test_text_garbage_returns_empty(monkeypatch):
    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout="not json at all"))
    assert claude_code.invoke(EngineRequest(prompt="p", timeout=5)).text == ""


def test_usage_parsed_from_envelope(monkeypatch):
    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout=ENVELOPE, returncode=0))
    r = claude_code.invoke(EngineRequest(prompt="p", timeout=5))
    assert r.cost_usd == 0.0231
    assert r.input_tokens == 1200 and r.output_tokens == 350
    assert r.cache_read_tokens == 9000 and r.cache_creation_tokens == 400


def test_usage_none_without_envelope(monkeypatch):
    """No session_id line → no envelope → accounting stays None, never 0."""
    _which_ok(monkeypatch)
    monkeypatch.setattr(claude_code.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout='{"result":"ok"}'))
    r = claude_code.invoke(EngineRequest(prompt="p", timeout=5))
    assert r.cost_usd is None and r.output_tokens is None


# ---------- resolve_model ----------


def test_resolve_model_default(monkeypatch):
    monkeypatch.delenv("ENGRAM_WATCHER_MODEL", raising=False)
    monkeypatch.delenv("ENGRAM_FORMATION_MODEL", raising=False)
    assert claude_code.resolve_model("formation") == "sonnet"
    assert claude_code.resolve_model() == "sonnet"


def test_resolve_model_per_role_beats_global(monkeypatch):
    monkeypatch.setenv("ENGRAM_WATCHER_MODEL", "sonnet")
    monkeypatch.setenv("ENGRAM_EVAL_MODEL", "haiku")
    assert claude_code.resolve_model("eval") == "haiku"
    assert claude_code.resolve_model("formation") == "sonnet"


def test_resolve_model_consolidation_is_none(monkeypatch):
    monkeypatch.setenv("ENGRAM_WATCHER_MODEL", "sonnet")
    assert claude_code.resolve_model("consolidation") is None


# ---------- prepare_sandbox ----------


def test_prepare_sandbox_watcher_spec(tmp_path):
    claude_code.prepare_sandbox(tmp_path, SandboxSpec(
        command_prefixes=("engram remember",),
        readable_paths=(str(tmp_path / "delta.txt"),),
    ))
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert data["permissions"]["allow"] == [
        "Bash(engram remember *)",
        f"Read({tmp_path / 'delta.txt'})",
    ]


def test_prepare_sandbox_consolidation_spec_matches_historic_grants(tmp_path):
    """The consolidation agent's allowlist is wire-frozen: this is the exact
    list write_agent_settings used to receive inline."""
    claude_code.prepare_sandbox(tmp_path, SandboxSpec(
        command_prefixes=("engram",),
        readonly_explore=True,
    ))
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert data["permissions"]["allow"] == [
        "Read", "Grep", "Glob",
        "Bash(engram *)", "Bash(sqlite3 *)",
        "Bash(wc *)", "Bash(head *)", "Bash(cat *)", "Bash(ls *)",
        "Bash(git log *)", "Bash(git diff *)", "Bash(git show *)",
        "Bash(git -C *)", "Bash(git rev-parse *)",
    ]
