"""Unit tests for the prompt override/loader chain.

User overrides live at <engram home>/prompts/, so tests steer the whole
chain through $ENGRAM_HOME — the same knob real installs use.
"""

from __future__ import annotations

import pytest

from toolengrams.prompts import loader


def _set_home(monkeypatch, tmp_path, with_prompts: bool = False):
    """Point the engram home at tmp_path; optionally create its prompts dir."""
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    prompts = tmp_path / "prompts"
    if with_prompts:
        prompts.mkdir(parents=True, exist_ok=True)
    return prompts


def test_packaged_default_is_found(tmp_path, monkeypatch):
    # Isolate from any real user override.
    _set_home(monkeypatch, tmp_path)
    p = loader.resolve_prompt_path("watcher")
    assert p.name == "watcher.md"
    assert "Memory fields" in p.read_text()


def test_user_override_takes_precedence_over_default(tmp_path, monkeypatch):
    prompts = _set_home(monkeypatch, tmp_path, with_prompts=True)
    (prompts / "watcher.md").write_text("CUSTOM WATCHER")
    result = loader.load_prompt("watcher")
    assert result == "CUSTOM WATCHER"


def test_env_var_takes_precedence_over_user_override(tmp_path, monkeypatch):
    prompts = _set_home(monkeypatch, tmp_path, with_prompts=True)
    (prompts / "watcher.md").write_text("USER OVERRIDE")

    env_file = tmp_path / "from-env.md"
    env_file.write_text("FROM ENV")
    monkeypatch.setenv("ENGRAM_WATCHER_PROMPT_PATH", str(env_file))

    assert loader.load_prompt("watcher") == "FROM ENV"


def test_env_var_with_missing_file_falls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_WATCHER_PROMPT_PATH", str(tmp_path / "missing.md"))
    _set_home(monkeypatch, tmp_path)
    # Falls through to packaged default.
    assert "Memory fields" in loader.load_prompt("watcher")


def test_interpolation_replaces_variables(tmp_path, monkeypatch):
    prompts = _set_home(monkeypatch, tmp_path, with_prompts=True)
    (prompts / "t.md").write_text("Hello {name}, target={target_date}")
    result = loader.load_prompt("t", name="world", target_date="2026-04-22")
    assert result == "Hello world, target=2026-04-22"


def test_missing_prompt_raises(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(loader, "_DEFAULTS_DIR", tmp_path / "also-nope")
    with pytest.raises(loader.PromptNotFound):
        loader.load_prompt("does-not-exist")


def test_tilde_in_env_var_is_expanded(tmp_path, monkeypatch):
    # Write a file under the real HOME. We won't actually use it — we just
    # verify that Path.expanduser is applied on the env-var path.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_home(monkeypatch, tmp_path / "engram-home")

    target = home / "override.md"
    target.write_text("TILDE WORKS")
    monkeypatch.setenv("ENGRAM_WATCHER_PROMPT_PATH", "~/override.md")
    assert loader.load_prompt("watcher") == "TILDE WORKS"


def test_consolidation_prompt_builds_with_variables():
    from toolengrams.prompts.consolidation import build_consolidation_prompt

    result = build_consolidation_prompt(
        session_list="- sess-1\n- sess-2",
        memory_summary="3 active memories",
        target_date="2026-04-22",
    )
    assert "2026-04-22" in result
    assert "sess-1" in result
    assert "3 active memories" in result


def test_watcher_prompt_builds():
    from toolengrams.prompts.watcher import build_watcher_prompt

    result = build_watcher_prompt()
    assert "kind" in result  # block/hint vocabulary
    assert "Without this memory" in result
