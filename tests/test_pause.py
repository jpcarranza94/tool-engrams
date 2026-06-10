"""Kill switch: `engram pause`/`resume`, $ENGRAM_DISABLED, hook short-circuit."""

from __future__ import annotations

import io
import json
import sys

from toolengrams import pause
from toolengrams.hooks import pretool
from toolengrams.watcher import tick


def _use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_DB", str(tmp_path / "db.sqlite"))
    monkeypatch.delenv("ENGRAM_DISABLED", raising=False)


def test_enabled_by_default(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    assert not pause.is_disabled()


def test_pause_resume_toggle_flag(tmp_path, monkeypatch, capsys):
    _use_tmp_db(tmp_path, monkeypatch)

    assert pause.run_pause() == 0
    assert pause.flag_path().exists()
    assert pause.is_disabled()
    assert json.loads(capsys.readouterr().out)["action"] == "paused"

    assert pause.run_resume() == 0
    assert not pause.flag_path().exists()
    assert not pause.is_disabled()
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "resumed"
    assert out["was_paused"] is True


def test_env_var_disables_without_flag(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ENGRAM_DISABLED", "1")
    assert pause.is_disabled()
    monkeypatch.setenv("ENGRAM_DISABLED", "true")
    assert pause.is_disabled()


def test_env_var_beats_flag_file(tmp_path, monkeypatch, capsys):
    _use_tmp_db(tmp_path, monkeypatch)
    pause.run_pause()
    capsys.readouterr()
    monkeypatch.setenv("ENGRAM_DISABLED", "0")
    assert not pause.is_disabled()  # explicit env enable overrides the flag


def test_resume_warns_when_env_still_set(tmp_path, monkeypatch, capsys):
    _use_tmp_db(tmp_path, monkeypatch)
    pause.run_pause()
    capsys.readouterr()
    monkeypatch.setenv("ENGRAM_DISABLED", "1")
    pause.run_resume()
    assert "warning" in json.loads(capsys.readouterr().out)


def test_pretool_short_circuits_when_paused(tmp_path, monkeypatch, capsys):
    _use_tmp_db(tmp_path, monkeypatch)
    pause.run_pause()
    capsys.readouterr()

    # Paused: the hook must emit {} without even reading stdin.
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    assert pretool.main() == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_watcher_tick_short_circuits_when_paused(tmp_path, monkeypatch, capsys):
    _use_tmp_db(tmp_path, monkeypatch)
    pause.run_pause()
    capsys.readouterr()

    # Paused: no usage error for missing args, no work — immediate 0.
    assert tick.main([]) == 0


def test_is_disabled_never_raises(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(pause, "flag_path", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert pause.is_disabled() is False
