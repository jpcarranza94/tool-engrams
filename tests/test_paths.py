"""Resolution order of the data home (paths.engram_home)."""

from pathlib import Path

from toolengrams import paths


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "custom"))
    monkeypatch.setattr(paths, "DEFAULT_HOME", tmp_path / "default")
    monkeypatch.setattr(paths, "LEGACY_HOME", tmp_path / "legacy")
    (tmp_path / "default").mkdir()
    assert paths.engram_home() == tmp_path / "custom"


def test_env_override_expands_user(monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", "~/somewhere")
    assert paths.engram_home() == Path.home() / "somewhere"


def test_default_home_wins_when_present(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    default = tmp_path / "default"
    legacy = tmp_path / "legacy"
    default.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(paths, "DEFAULT_HOME", default)
    monkeypatch.setattr(paths, "LEGACY_HOME", legacy)
    assert paths.engram_home() == default


def test_legacy_fallback_when_default_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(paths, "DEFAULT_HOME", tmp_path / "default")
    monkeypatch.setattr(paths, "LEGACY_HOME", legacy)
    assert paths.engram_home() == legacy


def test_fresh_install_uses_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    monkeypatch.setattr(paths, "DEFAULT_HOME", tmp_path / "default")
    monkeypatch.setattr(paths, "LEGACY_HOME", tmp_path / "legacy")
    assert paths.engram_home() == tmp_path / "default"
