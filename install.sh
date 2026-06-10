#!/usr/bin/env bash
set -euo pipefail

# ToolEngrams installer
# Installs the package, wires hooks into Claude Code, symlinks skills,
# initializes the database, and optionally installs the nightly schedule.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
SKILLS_DIR="$HOME/.claude/skills"
DB_DIR="$HOME/.claude/tool-engrams"

MIN_PYTHON="3.10"
MIN_CLAUDE="2.1.117"

# Reject unknown flags so a typo'd --uninstall can't silently run a full install.
if [ -n "${1:-}" ] && [ "${1}" != "--uninstall" ]; then
    echo "Usage: ./install.sh [--uninstall]"
    exit 2
fi

# --uninstall: remove what this script wired up (hooks, permission, skill
# symlinks). The DB and the Python package stay — memories are user data.
if [ "${1:-}" = "--uninstall" ]; then
    echo "ToolEngrams uninstaller (script-install path)"
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
        # Marker: the same "engram <subcommand>" commands install.sh writes.
        # Filter at the individual-hook level so a hand-merged entry mixing
        # engram with another tool's hook keeps the other tool's hooks.
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
        [ -L "$SKILLS_DIR/$link" ] && rm "$SKILLS_DIR/$link" && echo "  Removed skill symlink $link"
    done
    command -v engram &>/dev/null && engram consolidate --uninstall-schedule >/dev/null 2>&1 || true
    echo "  Done. Kept: the DB at $DB_DIR (your memories) and the Python package."
    if [ -d "$HOME/.local/share/toolengrams/venv" ]; then
        echo "  This was a venv-fallback install; to remove the package:"
        echo "    rm -rf ~/.local/share/toolengrams/venv ~/.local/bin/engram"
    else
        echo "  (pip uninstall toolengrams to remove the package.)"
    fi
    exit 0
fi

echo "ToolEngrams installer"
echo "====================="
echo ""

# 0. Preflight: required tool versions, with actionable errors.
echo "0. Checking prerequisites..."
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python >= $MIN_PYTHON first."
    echo "  macOS: brew install python3    Debian/Ubuntu: apt install python3 python3-pip"
    exit 1
fi
if ! python3 -c 'import sys
need = tuple(int(x) for x in sys.argv[1].split("."))
sys.exit(0 if sys.version_info[:len(need)] >= need else 1)' "$MIN_PYTHON"; then
    echo "ERROR: Python >= $MIN_PYTHON required, found $(python3 --version 2>&1)."
    exit 1
fi
echo "  $(python3 --version 2>&1) OK"

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
echo ""

# 1. Install the Python package.
#    Order: uv (if present) -> pip -> pip --user -> dedicated venv. PEP 668
#    "externally-managed-environment" rejects pip AND --user on stock
#    Debian/Ubuntu/Homebrew Python, so the venv fallback is the one that makes
#    a stock machine actually work — engram gets symlinked into ~/.local/bin.
VENV_DIR="$HOME/.local/share/toolengrams/venv"
echo "1. Installing toolengrams package..."
install_ok=0
if command -v uv &>/dev/null; then
    echo "  Trying uv..."
    if uv pip install --system -e "$REPO_DIR"; then
        install_ok=1
    else
        echo "  uv install failed (common with uv-managed Pythons); falling back to pip."
    fi
fi
if [ "$install_ok" -eq 0 ] && python3 -m pip --version &>/dev/null; then
    # `python3 -m pip` (not a bare pip3 shim) so the install targets the same
    # interpreter the version preflight just validated.
    if python3 -m pip install -e "$REPO_DIR"; then
        install_ok=1
    else
        echo "  pip install failed (PEP 668 externally-managed environment?). Retrying with --user..."
        if python3 -m pip install --user -e "$REPO_DIR"; then
            install_ok=1
        fi
    fi
fi
if [ "$install_ok" -eq 0 ]; then
    echo "  Falling back to a dedicated venv at $VENV_DIR..."
    # --clear so a partial venv left by an earlier failed run can't poison this one.
    if ! python3 -m venv --clear "$VENV_DIR"; then
        echo ""
        echo "ERROR: could not create a venv. On Debian/Ubuntu install it first:"
        echo "  apt install python3-venv python3-pip"
        echo "Then re-run this script. (Alternative: pipx install -e $REPO_DIR)"
        exit 1
    fi
    if ! "$VENV_DIR/bin/pip" install -e "$REPO_DIR"; then
        echo ""
        echo "ERROR: venv pip install failed — see the error above."
        echo "  (Alternative: pipx install -e $REPO_DIR)"
        exit 1
    fi
    mkdir -p "$HOME/.local/bin"
    ln -sf "$VENV_DIR/bin/engram" "$HOME/.local/bin/engram"
    echo "  Installed into $VENV_DIR; linked engram -> ~/.local/bin/engram"
    install_ok=1
    # The hooks invoke plain `engram`, so ~/.local/bin must be on PATH (it is
    # by default on Ubuntu login shells; the check below catches it if not).
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) export PATH="$HOME/.local/bin:$PATH" ;;
    esac
fi

# Rehash if using pyenv.
if command -v pyenv &>/dev/null; then
    pyenv rehash
fi

# Verify engram is on PATH — everything downstream (hooks, DB init) needs it.
if ! command -v engram &>/dev/null; then
    USER_BIN="$(python3 -m site --user-base)/bin"
    echo "ERROR: 'engram' is not on PATH after install."
    if [ -x "$USER_BIN/engram" ]; then
        echo "  It was installed to $USER_BIN (a --user install)."
        echo "  Add it to PATH and re-run: export PATH=\"$USER_BIN:\$PATH\""
    elif [ -x "$HOME/.local/bin/engram" ]; then
        echo "  It is linked at ~/.local/bin/engram, but ~/.local/bin is not on PATH."
        echo "  Add it to PATH and re-run: export PATH=\"\$HOME/.local/bin:\$PATH\""
    else
        echo "  Check the pip output above for where the 'engram' script was placed,"
        echo "  add that directory to PATH, and re-run this script."
    fi
    exit 1
fi
echo ""

# 2. Wire hooks into settings.json.
echo "2. Configuring Claude Code hooks..."
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
    # ln -sfn also re-points a stale symlink left by an older checkout.
    if [ -L "$link" ] || [ ! -e "$link" ]; then
        ln -sfn "$target" "$link"
        echo "  Linked $skill"
    else
        echo "  $skill exists as a real (non-symlink) path — left untouched"
    fi
done
echo ""

# 4. Initialize DB + verify the wiring. Opening the DB runs the migrations;
#    doctor then re-checks the hook wiring, PATH, claude version, and DB.
echo "4. Initializing database + verifying wiring..."
mkdir -p "$DB_DIR"
if ! engram doctor; then
    echo ""
    echo "ERROR: 'engram doctor' reported failures above — fix them and re-run this script."
    exit 1
fi
echo "  Database ready at $DB_DIR/db.sqlite"
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
# Prompt only on a tty: under `set -e` a non-interactive `read` dies on EOF,
# killing headless installs at the final step.
if [ "$OS" != "unsupported" ]; then
    if [ -t 0 ]; then
        read -p "   Install the 8 AM daily schedule? [y/N] " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            engram consolidate --install-schedule
            echo "   Schedule installed."
        else
            echo "   Skipped. Run 'engram consolidate --install-schedule' later."
        fi
    else
        echo "   Non-interactive install — skipped. Run 'engram consolidate --install-schedule' later."
    fi
fi
echo ""

# Done.
echo "====================="
echo "ToolEngrams installed!"
echo ""
echo "IMPORTANT: hooks load at session start — open a NEW Claude Code session."
echo "Sessions already running will not pick them up."
echo ""
echo "Verify it's working (see README, 'Verify it's working'):"
echo "  1. engram seed                       — plant demo memories"
echo "  2. In a NEW session, ask Claude to run: ssh deploy@production"
echo "  3. engram status                     — total_surfaces incremented"
echo "  4. engram seed --remove              — clean up the demos"
echo ""
echo "Commands:"
echo "  engram doctor          — wiring + liveness diagnostics"
echo "  engram recall          — browse memories"
echo "  engram dashboard       — visual overview (browser)"
echo "  engram monitor         — watcher activity + per-run cost"
echo "  engram status          — health summary"
echo "  engram consolidate     — run nightly consolidation now"
echo ""
echo "Memories form automatically as you use Claude Code. The first day can be"
echo "quiet — organic memories need real failure→recovery episodes. Watch the"
echo "watcher's decisions live with 'engram monitor'."
