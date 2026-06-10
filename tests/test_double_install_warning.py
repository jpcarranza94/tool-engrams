"""session_start's plugin/script double-install detection."""

from __future__ import annotations

import json

from toolengrams.hooks import session_start


def _write_settings(tmp_path, hooks):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps({"hooks": hooks}))


def test_warns_when_plugin_and_script_hooks_coexist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENGRAM_PLUGIN", "1")
    _write_settings(tmp_path, {"Stop": [{"hooks": [{"command": "engram stop"}]}]})
    assert "fires twice" in session_start._double_install_warning()


def test_silent_outside_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ENGRAM_PLUGIN", raising=False)
    _write_settings(tmp_path, {"Stop": [{"hooks": [{"command": "engram stop"}]}]})
    assert session_start._double_install_warning() == ""


def test_silent_when_no_script_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENGRAM_PLUGIN", "1")
    _write_settings(tmp_path, {"Stop": [{"hooks": [{"command": "other-tool stop"}]}]})
    assert session_start._double_install_warning() == ""


def test_silent_on_missing_or_malformed_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ENGRAM_PLUGIN", "1")
    assert session_start._double_install_warning() == ""  # missing file
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{broken")
    assert session_start._double_install_warning() == ""  # malformed
