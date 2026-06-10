"""Plugin packaging guards: manifest validity, hook wiring, script syntax.

The plugin manifests are dead JSON until someone installs the plugin — these
tests keep them honest against the code they point at.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"

HOOK_EVENTS = {
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "Stop", "SessionEnd", "PreCompact",
}

# engram subcommands dispatched as plain hook handlers in __main__.py.
HOOK_SUBCOMMANDS = {
    "session-start", "user-prompt", "pretool", "post-tool",
    "post-tool-failure", "stop", "flush",
}


def _plugin():
    return json.loads(PLUGIN_JSON.read_text())


def test_plugin_json_declares_all_eight_events():
    assert set(_plugin()["hooks"]) == HOOK_EVENTS


def test_plugin_json_identity_fields():
    plugin = _plugin()
    assert plugin["name"] == "tool-engrams"
    assert re.fullmatch(r"\d+\.\d+\.\d+", plugin["version"])
    # The system spends money once enabled — must not auto-enable on install.
    assert plugin["defaultEnabled"] is False


def test_plugin_hooks_route_through_shim_with_known_subcommands():
    for event, entries in _plugin()["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                cmd = hook["command"]
                assert "${CLAUDE_PLUGIN_ROOT}/plugin/hook.sh" in cmd, (event, cmd)
                assert '"${CLAUDE_PLUGIN_DATA}"' in cmd, (event, cmd)
                subcommand = cmd.rsplit(" ", 1)[-1]
                assert subcommand in HOOK_SUBCOMMANDS, (event, subcommand)
                # Plugin hook timeouts are SECONDS (settings.json legacy used ms).
                assert 1 <= hook["timeout"] <= 60, (event, hook["timeout"])


def test_marketplace_is_self_hosting_and_disabled_by_default():
    market = json.loads(MARKETPLACE_JSON.read_text())
    entries = {p["name"]: p for p in market["plugins"]}
    assert "tool-engrams" in entries
    assert entries["tool-engrams"]["source"] == "./"
    assert entries["tool-engrams"]["defaultEnabled"] is False


def test_plugin_shell_scripts_parse():
    for script in ("plugin/hook.sh", "plugin/bootstrap.sh"):
        proc = subprocess.run(["sh", "-n", str(REPO_ROOT / script)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{script}: {proc.stderr}"


def test_plugin_and_install_sh_wire_the_same_events():
    """plugin.json and install.sh define the same hook contract in two places;
    this tripwire fails when one gains/loses an event the other doesn't."""
    install_sh = (REPO_ROOT / "install.sh").read_text()
    for event in HOOK_EVENTS:
        assert f'"{event}"' in install_sh, (
            f"install.sh no longer wires {event} but plugin.json does")


def test_skill_folders_match_plugin_namespacing():
    """Plugin skills are /tool-engrams:<folder>; keep folders short, and keep
    SKILL.md free of a frontmatter name that would override them."""
    for folder in ("remember", "recall", "forget"):
        skill_md = REPO_ROOT / "skills" / folder / "SKILL.md"
        assert skill_md.is_file(), f"skills/{folder}/SKILL.md missing"
        frontmatter = skill_md.read_text().split("---")[1]
        assert not re.search(r"^name:", frontmatter, re.M), (
            f"skills/{folder}: frontmatter `name` would override the folder name "
            "for BOTH the plugin and the legacy symlinks — naming comes from the "
            "folder (plugin) / symlink (legacy) basename instead."
        )
