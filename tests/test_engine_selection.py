"""Engine selection precedence + the registry conformance contract."""

from __future__ import annotations

import json

from toolengrams.engine import ENGINES, EngineAdapter, get_engine
from toolengrams.engine import claude_code, codex, selection


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    monkeypatch.delenv("ENGRAM_ENGINE", raising=False)


def test_default_is_claude_code(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    assert get_engine() is claude_code


def test_override_argument_wins(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAM_ENGINE", "something-else")
    assert get_engine("claude-code") is claude_code


def test_env_var_beats_config(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"engine": "from-config"}))
    monkeypatch.setenv("ENGRAM_ENGINE", "claude-code")
    assert get_engine() is claude_code


def test_config_json_supplies_engine(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"engine": "claude-code"}))
    assert selection._config_engine() == "claude-code"
    assert get_engine() is claude_code


def test_codex_registered_and_selectable(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    assert get_engine("codex") is codex


def test_unknown_engine_warns_and_falls_back(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAM_ENGINE", "gpt-fax-machine")
    assert get_engine() is claude_code
    assert "unknown engine" in capsys.readouterr().err


def test_malformed_config_is_ignored(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{not json")
    assert get_engine() is claude_code


def test_registry_adapters_satisfy_protocol():
    """Every registered engine module must carry the full adapter surface —
    catches a forgotten function when a new harness lands."""
    for name, engine in ENGINES.items():
        assert isinstance(engine, EngineAdapter), f"{name} missing adapter attrs"
        assert engine.NAME == name
