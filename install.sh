#!/usr/bin/env bash
set -euo pipefail

# ToolEngrams installer
# Installs the package, wires hooks into Claude Code, symlinks skills,
# initializes the database, and optionally installs the nightly schedule.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
SKILLS_DIR="$HOME/.claude/skills"
DB_DIR="$HOME/.claude/tool-engrams"

echo "ToolEngrams installer"
echo "====================="
echo ""

# 1. Install the Python package.
echo "1. Installing toolengrams package..."
if command -v uv &>/dev/null; then
    uv pip install --system -e "$REPO_DIR" 2>&1 | tail -1
elif command -v pip &>/dev/null; then
    pip install -e "$REPO_DIR" 2>&1 | tail -1
else
    echo "ERROR: Neither uv nor pip found. Install one first."
    exit 1
fi

# Rehash if using pyenv.
if command -v pyenv &>/dev/null; then
    pyenv rehash
fi

# Verify engram is on PATH.
if ! command -v engram &>/dev/null; then
    echo "WARNING: 'engram' not found on PATH after install."
    echo "You may need to add your Python bin directory to PATH."
    echo "Try: export PATH=\"\$(python3 -m site --user-base)/bin:\$PATH\""
fi
echo ""

# 2. Wire hooks into settings.json.
echo "2. Configuring Claude Code hooks..."
mkdir -p "$(dirname "$SETTINGS")"

if [ ! -f "$SETTINGS" ]; then
    echo '{}' > "$SETTINGS"
fi

python3 << 'PYEOF'
import json
from pathlib import Path

settings_path = Path.home() / ".claude" / "settings.json"
settings = json.loads(settings_path.read_text())

# Ensure hooks section exists.
hooks = settings.setdefault("hooks", {})

# SessionStart
if not any("engram session-start" in str(h) for h in hooks.get("SessionStart", [])):
    hooks.setdefault("SessionStart", []).append({
        "hooks": [{
            "type": "command",
            "command": "engram session-start",
            "timeout": 5000,
            "statusMessage": "Loading memories...",
        }]
    })
    print("  Added SessionStart hook")
else:
    print("  SessionStart hook already present")

# PreToolUse
if not any("engram pretool" in str(h) for h in hooks.get("PreToolUse", [])):
    hooks.setdefault("PreToolUse", []).append({
        "matcher": "Bash|Read|Edit|Write|Grep|Glob|WebFetch|NotebookEdit",
        "hooks": [{
            "type": "command",
            "command": "engram pretool",
            "timeout": 3000,
            "statusMessage": "Recalling...",
        }]
    })
    print("  Added PreToolUse hook")
else:
    print("  PreToolUse hook already present")

# PostToolUse (success reinforcement + turn counter)
if not any("engram post-tool" in str(h) and "post-tool-failure" not in str(h)
           for h in hooks.get("PostToolUse", [])):
    hooks.setdefault("PostToolUse", []).append({
        "matcher": "Bash|Read|Edit|Write|Grep|Glob|WebFetch|NotebookEdit",
        "hooks": [{
            "type": "command",
            "command": "engram post-tool",
            "timeout": 3000,
        }]
    })
    print("  Added PostToolUse hook")
else:
    print("  PostToolUse hook already present")

# PostToolUseFailure (hint injection on real tool failures)
if not any("engram post-tool-failure" in str(h)
           for h in hooks.get("PostToolUseFailure", [])):
    hooks.setdefault("PostToolUseFailure", []).append({
        "matcher": "Bash|Read|Edit|Write|Grep|Glob|WebFetch|NotebookEdit",
        "hooks": [{
            "type": "command",
            "command": "engram post-tool-failure",
            "timeout": 3000,
        }]
    })
    print("  Added PostToolUseFailure hook")
else:
    print("  PostToolUseFailure hook already present")

# UserPromptSubmit — fires a watcher tick on a likely user correction
if not any("engram user-prompt" in str(h) for h in hooks.get("UserPromptSubmit", [])):
    hooks.setdefault("UserPromptSubmit", []).append({
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": "engram user-prompt",
            "timeout": 2000,
        }]
    })
    print("  Added UserPromptSubmit hook")
else:
    print("  UserPromptSubmit hook already present")

# Stop — primary event-driven watcher trigger (one tick per completed turn)
if not any("engram stop" in str(h) for h in hooks.get("Stop", [])):
    hooks.setdefault("Stop", []).append({
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": "engram stop",
            "timeout": 5000,
        }]
    })
    print("  Added Stop hook")
else:
    print("  Stop hook already present")

# SessionEnd + PreCompact — final watcher flush tick (process the tail)
for _event in ("SessionEnd", "PreCompact"):
    if not any("engram flush" in str(h) for h in hooks.get(_event, [])):
        hooks.setdefault(_event, []).append({
            "matcher": "",
            "hooks": [{
                "type": "command",
                "command": "engram flush",
                "timeout": 5000,
            }]
        })
        print(f"  Added {_event} hook")
    else:
        print(f"  {_event} hook already present")

# Permission for engram CLI.
perms = settings.setdefault("permissions", {}).setdefault("allow", [])
if "Bash(engram *)" not in perms:
    perms.append("Bash(engram *)")
    print("  Added Bash(engram *) permission")
else:
    print("  Bash(engram *) permission already present")

settings_path.write_text(json.dumps(settings, indent=2) + "\n")
print("  Settings saved")
PYEOF
echo ""

# 3. Symlink skills.
echo "3. Symlinking skills..."
mkdir -p "$SKILLS_DIR"
for skill in engram-remember engram-forget engram-recall; do
    target="$REPO_DIR/skills/$skill"
    link="$SKILLS_DIR/$skill"
    if [ -L "$link" ] || [ -d "$link" ]; then
        echo "  $skill already linked"
    else
        ln -sf "$target" "$link"
        echo "  Linked $skill"
    fi
done
echo ""

# 4. Initialize DB.
echo "4. Initializing database..."
mkdir -p "$DB_DIR"
engram status >/dev/null 2>&1
echo "  Database ready at $DB_DIR/db.sqlite"
echo "  (Run 'engram seed' if you want example memories to explore)"
echo ""

# 5. Optional: install nightly consolidation schedule.
echo "5. Nightly consolidation schedule"
echo "   The consolidation agent reviews yesterday's sessions at 8 AM."
OS="$(uname -s)"
case "$OS" in
    Darwin)
        echo "   Platform: macOS (will use launchd)"
        ;;
    Linux)
        echo "   Platform: Linux (will use cron)"
        if ! command -v crontab &>/dev/null; then
            echo "   WARNING: crontab not found. Install cron to use scheduled consolidation."
            echo "   You can always run 'engram consolidate' manually."
            echo ""
            OS="unsupported"
        fi
        ;;
    *)
        echo "   Platform: $OS (unsupported for scheduling)"
        echo "   You can run 'engram consolidate' manually anytime."
        echo ""
        OS="unsupported"
        ;;
esac
if [ "$OS" != "unsupported" ]; then
    read -p "   Install the 8 AM daily schedule? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        engram consolidate --install-schedule
        echo "   Schedule installed."
    else
        echo "   Skipped. Run 'engram consolidate --install-schedule' later."
    fi
fi
echo ""

# Done.
echo "====================="
echo "ToolEngrams installed!"
echo ""
echo "Commands:"
echo "  engram recall          — browse memories"
echo "  engram dashboard       — visual overview (browser)"
echo "  engram monitor         — resource usage"
echo "  engram status          — health summary"
echo "  engram consolidate     — run nightly consolidation now"
echo ""
echo "Memories will form automatically as you use Claude Code."
