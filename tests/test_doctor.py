"""engram doctor — wiring + liveness checks.

Pins the contract the installer and the README verify-walkthrough rely on:
the eight hook markers, the FAIL/WARN split (fresh-but-quiet installs must
exit 0), and the liveness signals read from session_turns / watcher_state.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from toolengrams import paths
from toolengrams.cli import doctor
from toolengrams.retrieval.session_state import increment_session_turn
from toolengrams.watcher import state as watcher_state


def _write_settings(home: Path, *, drop_event: str | None = None,
                    with_permission: bool = True) -> None:
    hooks = {}
    for event, marker in doctor.HOOK_MARKERS.items():
        if event == drop_event:
            continue
        hooks[event] = [{"hooks": [{"type": "command", "command": marker}]}]
    settings = {"hooks": hooks}
    if with_permission:
        settings["permissions"] = {"allow": [doctor.ENGRAM_PERMISSION]}
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps(settings))


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ---------- hooks check ----------


def test_hooks_pass_when_all_events_wired(fake_home):
    _write_settings(fake_home)
    result = doctor._check_hooks()
    assert result["status"] == doctor.PASS


def test_hooks_fail_when_event_missing(fake_home):
    _write_settings(fake_home, drop_event="PostToolUseFailure")
    result = doctor._check_hooks()
    assert result["status"] == doctor.FAIL
    assert "PostToolUseFailure" in result["detail"]


def test_hooks_fail_when_no_settings_file(fake_home):
    result = doctor._check_hooks()
    assert result["status"] == doctor.FAIL
    assert "install.sh" in result["detail"]


def test_post_tool_marker_does_not_match_post_tool_failure(fake_home):
    # "engram post-tool-failure" under PostToolUse must NOT satisfy the
    # "engram post-tool" marker — prefix matching would hide a miswiring.
    _write_settings(fake_home, drop_event="PostToolUse")
    settings_path = fake_home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["hooks"]["PostToolUse"] = [
        {"hooks": [{"type": "command", "command": "engram post-tool-failure"}]}
    ]
    settings_path.write_text(json.dumps(settings))
    result = doctor._check_hooks()
    assert result["status"] == doctor.FAIL
    assert "PostToolUse" in result["detail"]


def test_permission_warns_when_missing(fake_home):
    _write_settings(fake_home, with_permission=False)
    result = doctor._check_permission()
    assert result["status"] == doctor.WARN


# ---------- claude version check ----------


def test_claude_version_too_old_fails(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_claude_version", lambda: "2.1.100")
    result = doctor._check_claude_version()
    assert result["status"] == doctor.FAIL


def test_claude_version_new_enough_passes(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_claude_version", lambda: "2.2.0")
    result = doctor._check_claude_version()
    assert result["status"] == doctor.PASS


def test_claude_missing_fails(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor._check_claude_version()
    assert result["status"] == doctor.FAIL


def test_claude_unparseable_version_warns(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_claude_version", lambda: None)
    result = doctor._check_claude_version()
    assert result["status"] == doctor.WARN


# ---------- liveness ----------


def test_engine_check_passes_when_available(monkeypatch):
    monkeypatch.delenv("ENGRAM_ENGINE", raising=False)
    monkeypatch.setenv("ENGRAM_HOME", "/nonexistent-engram-home")
    monkeypatch.setattr(doctor.engine_selection.claude_code.shutil, "which",
                        lambda name: "/bin/claude")
    c = doctor._check_engine()
    assert c["status"] == doctor.PASS
    assert "claude-code" in c["detail"]


def test_engine_check_fails_on_unknown_engine(monkeypatch):
    monkeypatch.setenv("ENGRAM_ENGINE", "gpt-fax-machine")
    c = doctor._check_engine()
    assert c["status"] == doctor.FAIL
    assert "unknown" in c["detail"]


def test_engine_check_fails_when_binary_missing(monkeypatch):
    monkeypatch.delenv("ENGRAM_ENGINE", raising=False)
    monkeypatch.setenv("ENGRAM_HOME", "/nonexistent-engram-home")
    monkeypatch.setattr(doctor.engine_selection.claude_code.shutil, "which",
                        lambda name: None)
    c = doctor._check_engine()
    assert c["status"] == doctor.FAIL
    assert "not found" in c["detail"]


@pytest.fixture
def fake_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    monkeypatch.setattr(paths, "DEFAULT_HOME", tmp_path / "default")
    monkeypatch.setattr(paths, "LEGACY_HOME", tmp_path / "legacy")
    return tmp_path


def test_home_passes_fresh(fake_paths):
    c = doctor._check_home()
    assert c["status"] == doctor.PASS


def test_home_warns_on_legacy_location(fake_paths):
    (fake_paths / "legacy").mkdir()
    c = doctor._check_home()
    assert c["status"] == doctor.WARN
    assert "legacy" in c["detail"]


def test_home_warns_on_split_brain(fake_paths):
    (fake_paths / "default").mkdir()
    (fake_paths / "legacy").mkdir()
    c = doctor._check_home()
    assert c["status"] == doctor.WARN
    assert "also exists" in c["detail"]


def test_home_passes_after_migration_symlink(fake_paths):
    (fake_paths / "default").mkdir()
    (fake_paths / "legacy").symlink_to(fake_paths / "default")
    c = doctor._check_home()
    assert c["status"] == doctor.PASS


def test_hook_liveness_warns_on_fresh_db(temp_db):
    result = doctor._check_hook_liveness()
    assert result["status"] == doctor.WARN
    assert "NEW Claude Code session" in result["detail"]


def test_hook_liveness_passes_after_activity(temp_db):
    increment_session_turn(temp_db, "session-1", int(time.time()))
    result = doctor._check_hook_liveness()
    assert result["status"] == doctor.PASS


def test_watcher_liveness_warns_when_never_ticked(temp_db):
    result = doctor._check_watcher_liveness()
    assert result["status"] == doctor.WARN


def test_watcher_liveness_passes_after_tick(temp_db):
    now_ts = int(time.time())
    temp_db.execute(
        "INSERT INTO watcher_state (work_session_id, role, last_checked_ts, "
        "created_ts, last_tick_ts) VALUES ('s1', 'formation', ?, ?, ?)",
        (now_ts, now_ts, now_ts),
    )
    assert watcher_state.last_tick_ts_any() == now_ts
    result = doctor._check_watcher_liveness()
    assert result["status"] == doctor.PASS


# ---------- main ----------


def test_main_json_exit_codes(fake_home, temp_db, monkeypatch, capsys):
    _write_settings(fake_home)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_claude_version", lambda: "2.2.0")

    # Fresh-but-quiet install: WARNs only -> healthy exit 0.
    assert doctor.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    statuses = {c["name"]: c["status"] for c in payload["checks"]}
    assert statuses["hooks"] == doctor.PASS
    assert statuses["hook_liveness"] == doctor.WARN

    # A FAIL (missing hook event) flips the exit code.
    _write_settings(fake_home, drop_event="Stop")
    assert doctor.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_format_ago_buckets():
    assert doctor._format_ago(30) == "30s ago"
    assert doctor._format_ago(600) == "10 min ago"
    assert doctor._format_ago(7200) == "2 h ago"
    assert doctor._format_ago(200000) == "2 d ago"
