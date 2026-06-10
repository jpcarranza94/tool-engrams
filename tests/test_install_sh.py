"""install.sh contract tests — hook wiring completeness + --uninstall surgery.

The install script is the single install path; these pin the parts that
break silently: the set of wired hook events, and the marker-based
--uninstall settings surgery.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

HOOK_EVENTS = {
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "Stop", "SessionEnd", "PreCompact",
}


def test_install_sh_wires_all_eight_events():
    text = INSTALL_SH.read_text()
    for event in HOOK_EVENTS:
        assert f'"{event}"' in text, f"install.sh no longer wires {event}"


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
