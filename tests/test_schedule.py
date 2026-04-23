"""Unit tests for consolidation/schedule.py — plist generation + PATH resolution."""

from __future__ import annotations

from pathlib import Path

from toolengrams.consolidation import schedule


def test_plist_contains_engram_binary_path(monkeypatch):
    # Stub shutil.which so the test is deterministic regardless of host PATH.
    monkeypatch.setattr(
        schedule.shutil, "which",
        lambda name: f"/fake/bin/{name}" if name in ("engram", "claude") else None,
    )
    xml = schedule._generate_plist()
    assert "/fake/bin/engram" in xml


def test_plist_embeds_path_env_with_claude_and_engram_dirs(monkeypatch):
    monkeypatch.setattr(
        schedule.shutil, "which",
        lambda name: {"engram": "/opt/py/bin/engram", "claude": "/opt/cli/bin/claude"}.get(name),
    )
    xml = schedule._generate_plist()
    # Env block present
    assert "<key>EnvironmentVariables</key>" in xml
    assert "<key>PATH</key>" in xml
    # Both resolved dirs included
    assert "/opt/py/bin" in xml
    assert "/opt/cli/bin" in xml
    # Fallback Homebrew dir included for common tooling
    assert "/opt/homebrew/bin" in xml


def test_plist_still_fine_when_claude_not_resolvable(monkeypatch):
    # Install path is normal (engram present), but claude can't be found.
    # Plist should still generate — it just won't include claude's dir.
    monkeypatch.setattr(
        schedule.shutil, "which",
        lambda name: "/opt/py/bin/engram" if name == "engram" else None,
    )
    xml = schedule._generate_plist()
    assert "/opt/py/bin" in xml
    # PATH still has fallbacks so the job doesn't start with truly empty PATH.
    assert "/usr/bin" in xml


def test_resolve_plist_path_dedupes_when_same_dir(monkeypatch):
    """engram and claude in the same dir must not appear twice."""
    monkeypatch.setattr(
        schedule.shutil, "which",
        lambda name: "/shared/bin/" + name if name in ("engram", "claude") else None,
    )
    path = schedule._resolve_plist_path()
    assert path.count("/shared/bin") == 1


def test_plist_schedules_8am():
    xml = schedule._generate_plist()
    assert "<key>Hour</key>" in xml
    assert "<integer>8</integer>" in xml
    assert "<integer>0</integer>" in xml  # Minute


def test_plist_uses_yesterday_json_args():
    xml = schedule._generate_plist()
    assert "<string>--yesterday</string>" in xml
    assert "<string>--json</string>" in xml
    # Absent: the old --agent flag.
    assert "--agent" not in xml


def test_log_paths_in_plist(tmp_path, monkeypatch):
    xml = schedule._generate_plist()
    assert "/consolidate.log" in xml
    assert "/consolidate.err" in xml
