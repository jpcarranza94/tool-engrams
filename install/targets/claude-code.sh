#!/usr/bin/env bash
set -euo pipefail

# ToolEngrams target script: Claude Code.
# Called by ../install.sh with REPO_DIR/SETTINGS/SKILLS_DIR in the env and one
# argument: preflight | install | uninstall. Owns everything claude-specific
# on the TARGET side: binary version preflight, hook wiring into
# ~/.claude/settings.json, skill symlinks, and the uninstall surgery.

MIN_CLAUDE="2.1.117"
ACTION="${1:?usage: claude-code.sh preflight|install|uninstall}"

preflight() {
    if ! command -v claude &>/dev/null; then
        echo "ERROR: Claude Code CLI ('claude') not found on PATH."
        echo "  Install it first: https://claude.com/claude-code"
        exit 1
    fi
    CLAUDE_VERSION="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
    if [ -z "$CLAUDE_VERSION" ]; then
        echo "WARNING: could not parse 'claude --version' output; continuing."
    elif ! python3 -c 'import sys
have = [int(x) for x in sys.argv[1].split(".")[:3]]
need = [int(x) for x in sys.argv[2].split(".")[:3]]
sys.exit(0 if have >= need else 1)' "$CLAUDE_VERSION" "$MIN_CLAUDE"; then
        echo "ERROR: claude >= $MIN_CLAUDE required (for the hook events ToolEngrams uses), found $CLAUDE_VERSION."
        echo "  Update Claude Code, then re-run this script."
        exit 1
    else
        echo "  claude $CLAUDE_VERSION OK"
    fi
}

install() {
    echo "  Configuring Claude Code hooks..."
    mkdir -p "$(dirname "$SETTINGS")"

    # Back up only the FIRST time, so a re-run can't clobber the pre-engram
    # original with already-modified settings.
    if [ -f "$SETTINGS" ]; then
        if [ ! -f "$SETTINGS.bak" ]; then
            cp -p "$SETTINGS" "$SETTINGS.bak"
            echo "  Backed up settings.json to settings.json.bak"
        fi
    else
        echo '{}' > "$SETTINGS"
    fi

    python3 - "$SETTINGS" << 'PYEOF'
import json
import sys
from pathlib import Path

settings_path = Path(sys.argv[1])
settings = json.loads(settings_path.read_text())
hooks = settings.setdefault("hooks", {})

TOOL_MATCHER = "Bash|Read|Edit|Write|Grep|Glob|WebFetch|NotebookEdit"
# (event, marker, command, matcher, timeout, statusMessage)
WIRING = [
    ("SessionStart", "engram session-start",
     "engram session-start --target claude-code", None, 5000, "Loading memories..."),
    ("PreToolUse", "engram pretool",
     "engram pretool --target claude-code", TOOL_MATCHER, 3000, "Recalling..."),
    ("PostToolUse", "engram post-tool",
     "engram post-tool --target claude-code", TOOL_MATCHER, 3000, None),
    ("PostToolUseFailure", "engram post-tool-failure",
     "engram post-tool-failure --target claude-code", TOOL_MATCHER, 3000, None),
    ("UserPromptSubmit", "engram user-prompt",
     "engram user-prompt --target claude-code", "", 2000, None),
    ("Stop", "engram stop",
     "engram stop --target claude-code", "", 5000, None),
    ("SessionEnd", "engram flush",
     "engram flush --target claude-code", "", 5000, None),
    ("PreCompact", "engram flush",
     "engram flush --target claude-code", "", 5000, None),
]

for event, marker, command, matcher, timeout, status in WIRING:
    present = any(
        h.get("command", "") == marker or h.get("command", "").startswith(marker + " ")
        for entry in hooks.get(event, [])
        for h in entry.get("hooks", [])
        # post-tool's marker is a prefix of post-tool-failure's command; the
        # space-suffix startswith above already disambiguates ("engram
        # post-tool " does not prefix "engram post-tool-failure ...").
    )
    if present:
        print(f"  {event} hook already present")
        continue
    hook = {"type": "command", "command": command, "timeout": timeout}
    if status:
        hook["statusMessage"] = status
    entry = {"hooks": [hook]}
    if matcher is not None:
        entry["matcher"] = matcher
    hooks.setdefault(event, []).append(entry)
    print(f"  Added {event} hook")

perms = settings.setdefault("permissions", {}).setdefault("allow", [])
if "Bash(engram *)" not in perms:
    perms.append("Bash(engram *)")
    print("  Added Bash(engram *) permission")
else:
    print("  Bash(engram *) permission already present")

settings_path.write_text(json.dumps(settings, indent=2) + "\n")
print("  Settings saved")
PYEOF

    echo "  Symlinking skills..."
    mkdir -p "$SKILLS_DIR"
    for skill in engram-remember engram-forget engram-recall; do
        target="$REPO_DIR/skills/$skill"
        link="$SKILLS_DIR/$skill"
        # ln -sfn also re-points a stale symlink left by an older checkout.
        if [ -L "$link" ] || [ ! -e "$link" ]; then
            ln -sfn "$target" "$link"
            echo "  Linked $skill"
        else
            echo "  $skill exists as a real (non-symlink) path — left untouched"
        fi
    done
}

uninstall() {
    if [ -f "$SETTINGS" ]; then
        cp -p "$SETTINGS" "$SETTINGS.uninstall.bak"
        python3 - "$SETTINGS" << 'PYEOF'
import json
import sys
from pathlib import Path

settings_path = Path(sys.argv[1])
settings = json.loads(settings_path.read_text())
hooks = settings.get("hooks", {})
removed = 0
for event in list(hooks):
    kept_entries = []
    for entry in hooks[event]:
        entry_hooks = entry.get("hooks", [])
        # Marker: the same "engram <subcommand>" commands this script writes
        # (the --target suffix is covered by the prefix match). Filter at the
        # individual-hook level so a hand-merged entry mixing engram with
        # another tool's hook keeps the other tool's hooks.
        kept_hooks = [h for h in entry_hooks
                      if not h.get("command", "").startswith("engram ")]
        removed += len(entry_hooks) - len(kept_hooks)
        if kept_hooks:
            entry["hooks"] = kept_hooks
            kept_entries.append(entry)
    if kept_entries:
        hooks[event] = kept_entries
    else:
        hooks.pop(event, None)
perms = settings.get("permissions", {}).get("allow", [])
if "Bash(engram *)" in perms:
    perms.remove("Bash(engram *)")
    removed += 1
settings_path.write_text(json.dumps(settings, indent=2) + "\n")
print(f"  Removed {removed} engram hooks/permissions from settings.json")
PYEOF
    fi
    for link in engram-remember engram-forget engram-recall; do
        [ -L "$SKILLS_DIR/$link" ] && rm "$SKILLS_DIR/$link" && echo "  Removed skill symlink $link" || true
    done
}

"$ACTION"
