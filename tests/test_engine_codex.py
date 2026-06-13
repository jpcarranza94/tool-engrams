"""Tests for the codex engine adapter (engine/codex.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from toolengrams.engine import EngineRequest, SandboxSpec
from toolengrams.engine import codex

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex" / "engine"
EVENTS = (FIXTURE_DIR / "success-events.jsonl").read_text()
FAILURE_EVENTS = (FIXTURE_DIR / "failure-events.jsonl").read_text()


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _which_ok(monkeypatch):
    monkeypatch.setattr(codex.shutil, "which", lambda name: "/bin/codex")


def _isolate_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setenv("ENGRAM_DB", str(home / "db.sqlite"))
    return home


# ---------- invoke: argv construction + temp files ----------


def test_invoke_builds_argv_with_containment_flags_and_files(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        last_message = Path(argv[argv.index("-o") + 1])
        last_message.write_text("final codex message\n")
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        captured["schema_text"] = schema_path.read_text()
        return _Proc(stdout=EVENTS, returncode=0)

    _which_ok(monkeypatch)
    monkeypatch.setattr(codex.subprocess, "run", fake_run)

    result = codex.invoke(EngineRequest(
        prompt="remember this",
        timeout=30,
        model="gpt-5-codex",
        schema='{"type":"object"}',
        cwd=str(work_dir),
        env={"X": "1"},
    ))

    argv = captured["argv"]
    assert argv[:5] == ["/bin/codex", "exec", "--json", "--skip-git-repo-check",
                        "--ephemeral"]
    assert "-s" in argv and argv[argv.index("-s") + 1] == "workspace-write"
    configs = [argv[i + 1] for i, value in enumerate(argv) if value == "-c"]
    assert f'sandbox_workspace_write.writable_roots=["{home}","{work_dir}"]' in configs
    assert "sandbox_workspace_write.network_access=false" in configs
    assert "sandbox_workspace_write.exclude_slash_tmp=true" in configs
    assert "sandbox_workspace_write.exclude_tmpdir_env_var=true" in configs
    assert 'approval_policy="never"' in configs
    assert argv[argv.index("--cd") + 1] == str(work_dir)
    assert argv[argv.index("-m") + 1] == "gpt-5-codex"
    assert argv[-2:] == ["--", "remember this"]
    assert captured["schema_text"] == '{"type":"object"}'
    assert captured["kw"]["cwd"] == str(work_dir)
    assert captured["kw"]["env"] == {"X": "1"}
    assert captured["kw"]["timeout"] == 30

    assert result.ok is True
    assert result.engine == "codex"
    assert result.stdout == EVENTS
    assert result.text == "final codex message\n"
    assert result.cost_usd is None
    assert result.input_tokens == 91896
    assert result.output_tokens == 343
    assert result.cache_read_tokens == 66688
    assert result.cache_creation_tokens is None


def test_invoke_omits_model_and_schema_when_unset(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        Path(argv[argv.index("-o") + 1]).write_text("ok")
        return _Proc(stdout=EVENTS)

    _which_ok(monkeypatch)
    monkeypatch.setattr(codex.subprocess, "run", fake_run)
    monkeypatch.delenv("ENGRAM_CODEX_WATCHER_MODEL", raising=False)
    monkeypatch.delenv("ENGRAM_CODEX_FORMATION_MODEL", raising=False)

    result = codex.invoke(EngineRequest(prompt="p", timeout=10, role="formation",
                                        cwd=str(work_dir)))

    assert result.ok is True
    assert "-m" not in captured["argv"]
    assert "--output-schema" not in captured["argv"]


# ---------- invoke: failure modes (never raises) ----------


def test_invoke_timeout_returns_flag(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw["timeout"])

    _which_ok(monkeypatch)
    monkeypatch.setattr(codex.subprocess, "run", boom)
    result = codex.invoke(EngineRequest(prompt="p", timeout=5, cwd=str(work_dir)))
    assert result.ok is False and result.timed_out is True
    assert result.returncode == 1 and "timed out" in result.error


def test_invoke_spawn_error_returns_flag(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    def boom(*a, **k):
        raise OSError("exec failed")

    _which_ok(monkeypatch)
    monkeypatch.setattr(codex.subprocess, "run", boom)
    result = codex.invoke(EngineRequest(prompt="p", timeout=5, cwd=str(work_dir)))
    assert result.ok is False and result.timed_out is False
    assert result.returncode == 1 and result.error.startswith("failed to spawn")


def test_invoke_missing_binary(monkeypatch):
    monkeypatch.setattr(codex.shutil, "which", lambda name: None)
    result = codex.invoke(EngineRequest(prompt="p", timeout=5))
    assert result.ok is False and result.error == "codex CLI not found on PATH"
    assert codex.is_available() is False


def test_invoke_requires_explicit_cwd(monkeypatch):
    _which_ok(monkeypatch)
    result = codex.invoke(EngineRequest(prompt="p", timeout=5))
    assert result.ok is False
    assert result.error == "codex engine requires an explicit cwd"


def test_nonzero_exit_surfaces_stderr_tail(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _which_ok(monkeypatch)
    monkeypatch.setattr(codex.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout="", returncode=1,
                                                 stderr="Error: turn failed\n"))
    result = codex.invoke(EngineRequest(prompt="p", timeout=5, cwd=str(work_dir)))
    assert result.ok is False and result.returncode == 1
    assert "exit 1" in result.error and "turn failed" in result.error
    assert "\n" not in result.error


def test_nonzero_exit_uses_event_error_when_stderr_empty(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _which_ok(monkeypatch)
    monkeypatch.setattr(codex.subprocess, "run",
                        lambda argv, **kw: _Proc(stdout=FAILURE_EVENTS,
                                                 returncode=1, stderr=""))

    result = codex.invoke(EngineRequest(prompt="p", timeout=5, cwd=str(work_dir)))

    assert result.ok is False and result.returncode == 1
    assert "sandbox denied write outside writable_roots" in result.error
    assert codex._event_error(FAILURE_EVENTS) == (
        "sandbox denied write outside writable_roots"
    )


# ---------- result parsing ----------


def test_usage_none_without_turn_completed(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    def fake_run(argv, **kw):
        Path(argv[argv.index("-o") + 1]).write_text("ok")
        return _Proc(stdout='{"type":"turn.started"}')

    _which_ok(monkeypatch)
    monkeypatch.setattr(codex.subprocess, "run", fake_run)
    result = codex.invoke(EngineRequest(prompt="p", timeout=5, cwd=str(work_dir)))
    assert result.cost_usd is None
    assert result.input_tokens is None and result.output_tokens is None


# ---------- resolve_model ----------


def test_resolve_model_default_is_none(monkeypatch):
    monkeypatch.delenv("ENGRAM_CODEX_WATCHER_MODEL", raising=False)
    monkeypatch.delenv("ENGRAM_CODEX_FORMATION_MODEL", raising=False)
    assert codex.resolve_model("formation") is None
    assert codex.resolve_model() is None


def test_resolve_model_per_role_beats_codex_global(monkeypatch):
    monkeypatch.setenv("ENGRAM_CODEX_WATCHER_MODEL", "gpt-5")
    monkeypatch.setenv("ENGRAM_CODEX_EVAL_MODEL", "gpt-5-mini")
    assert codex.resolve_model("eval") == "gpt-5-mini"
    assert codex.resolve_model("formation") == "gpt-5"


def test_resolve_model_consolidation_is_none(monkeypatch):
    monkeypatch.setenv("ENGRAM_CODEX_WATCHER_MODEL", "gpt-5")
    assert codex.resolve_model("consolidation") is None


# ---------- prepare_sandbox ----------


def test_prepare_sandbox_does_not_write_trust_gated_config(tmp_path):
    codex.prepare_sandbox(tmp_path, SandboxSpec(
        command_prefixes=("engram remember",),
        readable_paths=(str(tmp_path / "delta.txt"),),
    ))
    assert not (tmp_path / ".codex").exists()
