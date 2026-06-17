"""Unit tests for the durable config file (toolengrams/config.py) and the
`engram config` / `engram engine` CLI verbs."""

from __future__ import annotations

import json

import pytest

from toolengrams import config
from toolengrams.cli import config_cmd, engine_cmd
from toolengrams.engine import claude_code as engine_claude, codex as engine_codex


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point the config file at a tmp home and yield its path."""
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    return tmp_path / "config.json"


def _write(cfg_home, data):
    cfg_home.write_text(json.dumps(data))


# ---------- load / get ----------


def test_load_failopen_on_missing_and_garbage(cfg_home):
    assert config.load() == {}                 # missing file
    cfg_home.write_text("{not json")
    assert config.load() == {}                 # malformed → still {}


def test_get_reads_nested_key(cfg_home):
    _write(cfg_home, {"engines": {"codex": {"eval_model": "gpt-5"}}})
    assert config.get("engines.codex.eval_model") == "gpt-5"
    assert config.get("engines.codex.watcher_model") is None


# ---------- hydrate_env ----------


def test_hydrate_sets_missing_env(cfg_home, monkeypatch):
    monkeypatch.delenv("ENGRAM_ENGINE", raising=False)
    monkeypatch.delenv("ENGRAM_CODEX_EVAL_MODEL", raising=False)
    _write(cfg_home, {"engine": "codex",
                      "engines": {"codex": {"eval_model": "gpt-5"}}})

    config.hydrate_env()

    import os
    assert os.environ["ENGRAM_ENGINE"] == "codex"
    assert os.environ["ENGRAM_CODEX_EVAL_MODEL"] == "gpt-5"


def test_hydrate_does_not_override_explicit_env(cfg_home, monkeypatch):
    # Explicit env beats the file (precedence: env > file).
    monkeypatch.setenv("ENGRAM_ENGINE", "claude-code")
    _write(cfg_home, {"engine": "codex"})

    config.hydrate_env()

    import os
    assert os.environ["ENGRAM_ENGINE"] == "claude-code"


def test_hydrate_coerces_int_to_str(cfg_home, monkeypatch):
    monkeypatch.delenv("ENGRAM_TICK_COALESCE_SEC", raising=False)
    _write(cfg_home, {"watcher": {"tick_coalesce_sec": 30}})

    config.hydrate_env()

    import os
    # env vars are strings; downstream code does int(os.environ.get(...)).
    assert os.environ["ENGRAM_TICK_COALESCE_SEC"] == "30"


def test_hydrate_noop_on_empty(cfg_home):
    config.hydrate_env()  # no file → must not raise


# ---------- set / unset round-trip ----------


def test_set_get_unset_roundtrip(cfg_home):
    config.set_value("engine", "codex")
    assert json.loads(cfg_home.read_text())["engine"] == "codex"
    assert config.get("engine") == "codex"

    config.set_value("watcher.timeout", "1800")
    assert json.loads(cfg_home.read_text())["watcher"]["timeout"] == 1800  # int

    assert config.unset("watcher.timeout") is True
    assert config.get("watcher.timeout") is None
    # Empty parent container pruned, not left as {}.
    assert "watcher" not in json.loads(cfg_home.read_text())
    assert config.unset("watcher.timeout") is False  # already gone


def test_set_rejects_unknown_key(cfg_home):
    with pytest.raises(KeyError):
        config.set_value("bogus.key", "x")


def test_set_rejects_bad_int(cfg_home):
    with pytest.raises(ValueError):
        config.set_value("watcher.timeout", "not-a-number")


def test_unset_rejects_unknown_key(cfg_home):
    with pytest.raises(KeyError):
        config.unset("bogus.key")


# ---------- drift guard ----------


def test_spec_matches_engine_adapters():
    """Per-engine model keys must map to the env names the adapters actually
    read — otherwise a config value silently never reaches model resolution."""
    assert config.env_for("engines.codex.formation_model") == engine_codex._ROLE_MODEL_ENV["formation"]
    assert config.env_for("engines.codex.eval_model") == engine_codex._ROLE_MODEL_ENV["eval"]
    assert config.env_for("engines.claude-code.formation_model") == engine_claude._ROLE_MODEL_ENV["formation"]
    assert config.env_for("engines.claude-code.eval_model") == engine_claude._ROLE_MODEL_ENV["eval"]
    assert config.env_for("engines.codex.watcher_model") == "ENGRAM_CODEX_WATCHER_MODEL"
    assert config.env_for("engines.claude-code.watcher_model") == "ENGRAM_WATCHER_MODEL"


# ---------- CLI: engram config ----------


def test_config_cli_set_get_show(cfg_home, capsys):
    assert config_cmd.main(["set", "watcher.timeout", "900"]) == 0
    capsys.readouterr()

    assert config_cmd.main(["get", "watcher.timeout"]) == 0
    assert capsys.readouterr().out.strip() == "900"

    assert config_cmd.main(["show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out.strip())
    row = next(r for r in shown["settings"] if r["key"] == "watcher.timeout")
    assert row["effective"] == 900 and row["source"] == "file"


def test_config_cli_set_unknown_key_exits_2(cfg_home, capsys):
    assert config_cmd.main(["set", "nope.nope", "x"]) == 2
    assert "unknown key" in capsys.readouterr().err


def test_config_cli_show_marks_env_override(cfg_home, monkeypatch, capsys):
    config.set_value("engine", "claude-code")
    monkeypatch.setenv("ENGRAM_ENGINE", "codex")

    assert config_cmd.main(["show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out.strip())
    row = next(r for r in shown["settings"] if r["key"] == "engine")
    assert row["file"] == "claude-code"
    assert row["effective"] == "codex" and row["source"] == "env"


# ---------- CLI: engram engine ----------


def test_engine_cli_set_writes_config(cfg_home, capsys):
    assert engine_cmd.main(["set", "codex"]) == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["engine"] == "codex"
    assert config.get("engine") == "codex"


def test_engine_cli_set_unknown_exits_2(cfg_home, capsys):
    assert engine_cmd.main(["set", "gpt-9000"]) == 2
    assert "unknown engine" in capsys.readouterr().err
