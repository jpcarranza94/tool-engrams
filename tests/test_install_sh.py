"""install.sh contract tests — hook wiring completeness + --uninstall surgery.

The install script is the single install path; these pin the parts that
break silently: the set of wired hook events, and the marker-based
--uninstall settings surgery.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
CLAUDE_TARGET_SH = REPO_ROOT / "install" / "targets" / "claude-code.sh"
CODEX_ENGINE_SH = REPO_ROOT / "install" / "engines" / "codex.sh"
CODEX_TARGET_SH = REPO_ROOT / "install" / "targets" / "codex.sh"

HOOK_EVENTS = {
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "Stop", "SessionEnd", "PreCompact",
}


def test_claude_target_script_wires_all_eight_events():
    text = CLAUDE_TARGET_SH.read_text()
    for event in HOOK_EVENTS:
        assert f'"{event}"' in text, f"claude-code.sh no longer wires {event}"


def test_wired_commands_carry_target_flag():
    text = CLAUDE_TARGET_SH.read_text()
    assert "engram pretool --target claude-code" in text
    assert "engram flush --target claude-code" in text


def test_codex_target_script_wires_supported_events_and_features(tmp_path):
    codex_dir = tmp_path / ".codex"
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
           "CODEX_CONFIG": str(codex_dir / "config.toml"),
           "CODEX_HOOKS": str(codex_dir / "hooks.json")}

    proc = subprocess.run(["bash", str(CODEX_TARGET_SH), "install"],
                          capture_output=True, text=True, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "trust" in proc.stdout.lower()
    assert "[features]" in (codex_dir / "config.toml").read_text()
    assert "hooks = true" in (codex_dir / "config.toml").read_text()
    hooks = json.loads((codex_dir / "hooks.json").read_text())["hooks"]
    assert set(hooks) == {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "Stop", "PreCompact",
    }
    assert "PostToolUseFailure" not in hooks
    assert "SessionEnd" not in hooks
    cmds = [h["command"] for entries in hooks.values()
            for entry in entries for h in entry["hooks"]]
    assert "engram pretool --target codex" in cmds
    assert "engram flush --target codex" in cmds


def test_codex_target_script_does_not_treat_bare_engram_as_wired(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    hooks_path = codex_dir / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": {
        "PreToolUse": [{"hooks": [
            {"type": "command", "command": "engram pretool"},
        ]}],
    }}))
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
           "CODEX_CONFIG": str(codex_dir / "config.toml"),
           "CODEX_HOOKS": str(hooks_path)}

    proc = subprocess.run(["bash", str(CODEX_TARGET_SH), "install"],
                          capture_output=True, text=True, env=env)

    assert proc.returncode == 0, proc.stderr
    hooks = json.loads(hooks_path.read_text())["hooks"]
    pretool_cmds = [h["command"] for e in hooks["PreToolUse"] for h in e["hooks"]]
    assert "engram pretool" in pretool_cmds
    assert "engram pretool --target codex" in pretool_cmds


def test_codex_target_uninstall_strips_only_engram_hooks(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    hooks_path = codex_dir / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [
            {"type": "command", "command": "engram stop --target codex"},
            {"type": "command", "command": "engram something-else"},
            {"type": "command", "command": "other stop"},
        ]}],
        "PreToolUse": [{"hooks": [
            {"type": "command", "command": "engram pretool --target codex"},
        ]}],
    }}))
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
           "CODEX_HOOKS": str(hooks_path)}

    proc = subprocess.run(["bash", str(CODEX_TARGET_SH), "uninstall"],
                          capture_output=True, text=True, env=env)

    assert proc.returncode == 0, proc.stderr
    hooks = json.loads(hooks_path.read_text())["hooks"]
    assert "PreToolUse" not in hooks
    stop_cmds = [h["command"] for e in hooks["Stop"] for h in e["hooks"]]
    assert stop_cmds == ["engram something-else", "other stop"]


def test_migration_runs_before_engine_persistence():
    """Ordering pin for a real stranding bug: anything that creates $DB_DIR
    (the config.json engine write) must come AFTER migrate_legacy_home, whose
    [ ! -e "$DB_DIR" ] guard otherwise sees the fresh dir and never migrates
    the legacy home."""
    text = INSTALL_SH.read_text()
    migrate_call = text.index("\nmigrate_legacy_home\n")
    config_write = text.index("config.json")
    assert migrate_call < config_write


def test_target_install_idempotent_over_preseam_wiring(tmp_path):
    """Re-running the installer over OLD-format wiring (commands without
    --target) must report already-present — never double-wire."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({
        "hooks": {"Stop": [
            {"matcher": "", "hooks": [
                {"type": "command", "command": "engram stop", "timeout": 5000}]}
        ]},
        "permissions": {"allow": ["Bash(engram *)"]},
    }))
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
           "REPO_DIR": str(REPO_ROOT), "SETTINGS": str(settings_path),
           "SKILLS_DIR": str(claude_dir / "skills")}
    proc = subprocess.run(["bash", str(CLAUDE_TARGET_SH), "install"],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    settings = json.loads(settings_path.read_text())
    stop_cmds = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]]
    assert stop_cmds == ["engram stop"]          # untouched, not double-wired
    assert "Stop hook already present" in proc.stdout


def test_install_sh_rejects_unknown_target(tmp_path):
    proc = subprocess.run(
        ["bash", str(INSTALL_SH), "--target", "betamax"],
        capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 2
    assert "unknown target" in proc.stdout


def test_codex_engine_script_preflight_reports_missing_binary(tmp_path):
    proc = subprocess.run(
        ["bash", str(CODEX_ENGINE_SH), "preflight"],
        capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 1
    assert "codex" in proc.stdout
    assert "codex login" in proc.stdout


def test_codex_engine_script_preflight_passes_with_binary(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_bin = bin_dir / "codex"
    codex_bin.write_text("#!/usr/bin/env bash\necho 'codex 0.137.0'\n")
    codex_bin.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(CODEX_ENGINE_SH), "preflight"],
        capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert proc.returncode == 0
    assert "engine codex: codex 0.137.0 OK" in proc.stdout


def test_codex_engine_script_preflight_rejects_old_binary(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_bin = bin_dir / "codex"
    codex_bin.write_text("#!/usr/bin/env bash\necho 'codex 0.136.9'\n")
    codex_bin.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(CODEX_ENGINE_SH), "preflight"],
        capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert proc.returncode == 1
    assert "too old" in proc.stdout
    assert "0.137.0" in proc.stdout


def test_codex_engine_script_install_is_noop(tmp_path):
    proc = subprocess.run(
        ["bash", str(CODEX_ENGINE_SH), "install"],
        capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


# ---------- home migration (behavioral: runs the real function bytes) ----------


def _run_migration(home: Path, engram_home: str | None = None):
    """Extract migrate_legacy_home + the real LEGACY_DIR/DB_DIR definitions
    from install.sh and run them under a fake $HOME — the actual script
    lines, not a copy that could drift."""
    text = INSTALL_SH.read_text()
    fn = re.search(r"^migrate_legacy_home\(\) \{.*?^\}", text, re.S | re.M)
    legacy_def = re.search(r'^LEGACY_DIR=.*$', text, re.M)
    db_def = re.search(r'^DB_DIR=.*$', text, re.M)
    assert fn and legacy_def and db_def, "install.sh migration block went missing"
    script = "\n".join([
        "set -euo pipefail",
        legacy_def.group(0),
        db_def.group(0),
        fn.group(0),
        "migrate_legacy_home",
    ])
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if engram_home is not None:
        env["ENGRAM_HOME"] = engram_home
    return subprocess.run(["bash", "-c", script],
                          capture_output=True, text=True, env=env)


def test_migration_moves_legacy_and_symlinks_back(tmp_path):
    legacy = tmp_path / ".claude" / "tool-engrams"
    legacy.mkdir(parents=True)
    (legacy / "db.sqlite").write_text("sentinel")

    proc = _run_migration(tmp_path)

    assert proc.returncode == 0, proc.stderr
    new_home = tmp_path / ".tool-engrams"
    assert (new_home / "db.sqlite").read_text() == "sentinel"
    assert legacy.is_symlink()
    # Both paths resolve to the same data.
    assert (legacy / "db.sqlite").read_text() == "sentinel"


def test_migration_is_idempotent(tmp_path):
    legacy = tmp_path / ".claude" / "tool-engrams"
    legacy.mkdir(parents=True)
    (legacy / "db.sqlite").write_text("sentinel")

    assert _run_migration(tmp_path).returncode == 0
    proc = _run_migration(tmp_path)  # second run: legacy is now a symlink

    assert proc.returncode == 0, proc.stderr
    assert "Migrating" not in proc.stdout
    assert (tmp_path / ".tool-engrams" / "db.sqlite").read_text() == "sentinel"


def test_migration_warns_and_keeps_both_when_both_exist(tmp_path):
    legacy = tmp_path / ".claude" / "tool-engrams"
    legacy.mkdir(parents=True)
    (legacy / "db.sqlite").write_text("old")
    new_home = tmp_path / ".tool-engrams"
    new_home.mkdir()
    (new_home / "db.sqlite").write_text("new")

    proc = _run_migration(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stdout
    assert (legacy / "db.sqlite").read_text() == "old"      # untouched
    assert (new_home / "db.sqlite").read_text() == "new"    # untouched
    assert not legacy.is_symlink()


def test_migration_skipped_but_warned_when_engram_home_set(tmp_path):
    legacy = tmp_path / ".claude" / "tool-engrams"
    legacy.mkdir(parents=True)
    (legacy / "db.sqlite").write_text("sentinel")

    proc = _run_migration(tmp_path, engram_home=str(tmp_path / "custom"))

    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stdout
    assert "NOT be migrated" in proc.stdout
    assert legacy.is_dir() and not legacy.is_symlink()       # data untouched


def test_migration_noop_on_fresh_machine(tmp_path):
    proc = _run_migration(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    assert not (tmp_path / ".tool-engrams").exists()


def test_install_sh_rejects_unknown_flags(tmp_path):
    proc = subprocess.run(
        ["bash", str(INSTALL_SH), "--uninstal"],  # typo'd flag
        capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 2
    assert "Usage" in proc.stdout


def test_uninstall_removes_exactly_the_engram_entries(tmp_path):
    claude_dir = tmp_path / ".claude"
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(parents=True)

    settings = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "engram stop"}]},
                {"hooks": [{"type": "command", "command": "other-tool stop"}]},
            ],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "engram pretool"}]},
            ],
        },
        "permissions": {"allow": ["Bash(engram *)", "Bash(other *)"]},
    }
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings))

    # Fake skill symlinks the uninstaller should remove.
    for skill in ("engram-remember", "engram-forget", "engram-recall"):
        (skills_dir / skill).symlink_to(REPO_ROOT / "skills" / skill)

    proc = subprocess.run(
        ["bash", str(INSTALL_SH), "--uninstall"],
        capture_output=True, text=True,
        # Minimal PATH: python3 for the surgery, no engram so the
        # consolidation-schedule step is skipped.
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = json.loads(settings_path.read_text())
    assert "PreToolUse" not in after["hooks"]  # only engram entries → event dropped
    stop_cmds = [h["command"] for e in after["hooks"]["Stop"] for h in e["hooks"]]
    assert stop_cmds == ["other-tool stop"]  # foreign hook survives
    assert after["permissions"]["allow"] == ["Bash(other *)"]

    assert (claude_dir / "settings.json.uninstall.bak").exists()
    assert not any(skills_dir.iterdir())  # all three symlinks removed


def test_uninstall_preserves_foreign_hooks_in_mixed_entries(tmp_path):
    """A hand-merged entry mixing engram with another tool's hook keeps the
    other tool's hook — surgery is per-hook, not per-entry."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)

    settings = {
        "hooks": {
            "Stop": [
                {"hooks": [
                    {"type": "command", "command": "engram stop"},
                    {"type": "command", "command": "other-tool stop"},
                ]},
            ],
        },
    }
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings))

    proc = subprocess.run(
        ["bash", str(INSTALL_SH), "--uninstall"],
        capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = json.loads(settings_path.read_text())
    stop_cmds = [h["command"] for e in after["hooks"]["Stop"] for h in e["hooks"]]
    assert stop_cmds == ["other-tool stop"]


def test_schedule_prompt_is_tty_gated():
    """`read -p` under `set -e` kills non-interactive installs at the final
    step; the prompt must be guarded by a tty check."""
    text = INSTALL_SH.read_text()
    read_idx = text.index("read -p")
    gate_idx = text.index("[ -t 0 ]")
    assert gate_idx < read_idx, "schedule prompt not guarded by [ -t 0 ]"


def test_step4_runs_doctor():
    text = INSTALL_SH.read_text()
    assert "engram doctor" in text, "install.sh step 4 no longer verifies via doctor"
