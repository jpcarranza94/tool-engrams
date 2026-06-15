#!/usr/bin/env bash
set -euo pipefail

# ToolEngrams installer — the centralized trunk.
# Shared steps (python preflight, package install, data-home migration, DB
# init, doctor, schedule) live here; everything harness-specific lives in
# per-harness scripts dispatched on the --target / --engine flags:
#   install/targets/<name>.sh   hook wiring + uninstall surgery per TARGET
#   install/engines/<name>.sh   preflight per ENGINE
#
# Usage: ./install.sh [--target <name>]... [--engine <name>] [--uninstall]
# Defaults (--target claude-code --engine claude-code) keep the historic
# one-command install byte-compatible.

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
SKILLS_DIR="$HOME/.claude/skills"
export REPO_DIR SETTINGS SKILLS_DIR
# Data home: $ENGRAM_HOME overrides; default is the harness-neutral
# ~/.tool-engrams (the legacy ~/.claude/tool-engrams is migrated below).
LEGACY_DIR="$HOME/.claude/tool-engrams"
DB_DIR="${ENGRAM_HOME:-$HOME/.tool-engrams}"

# One-time migration of the legacy data home into $DB_DIR. Move, then leave a
# symlink behind so anything still resolving the legacy path (running
# sessions, older checkouts) lands on the same data. Note: a cross-filesystem
# mv degrades to copy+unlink — if a stray hook holds the sqlite open mid-copy
# the copy can tear; in the default layout both paths share $HOME.
# tests/test_install_sh.py extracts and runs this function — keep it
# self-contained (only $LEGACY_DIR / $DB_DIR / $ENGRAM_HOME).
migrate_legacy_home() {
    if [ -n "${ENGRAM_HOME:-}" ]; then
        if [ -d "$LEGACY_DIR" ] && [ ! -L "$LEGACY_DIR" ]; then
            echo "  WARNING: \$ENGRAM_HOME is set ($DB_DIR) but your existing data is at $LEGACY_DIR."
            echo "           It will NOT be migrated automatically — move it yourself or unset ENGRAM_HOME."
        fi
        return 0
    fi
    if [ -d "$LEGACY_DIR" ] && [ ! -L "$LEGACY_DIR" ]; then
        if [ ! -e "$DB_DIR" ]; then
            echo "  Migrating data home: $LEGACY_DIR -> $DB_DIR"
            mv "$LEGACY_DIR" "$DB_DIR"
            # An old-package hook can recreate the legacy dir between mv and
            # ln (their mkdir is exist_ok=True) — warn instead of dying
            # mid-install under set -e.
            if ! ln -s "$DB_DIR" "$LEGACY_DIR" 2>/dev/null; then
                echo "  WARNING: couldn't leave a compatibility symlink at $LEGACY_DIR."
                echo "           Re-link manually: rm -rf '$LEGACY_DIR' && ln -s '$DB_DIR' '$LEGACY_DIR'"
            fi
        else
            echo "  WARNING: both $DB_DIR and $LEGACY_DIR exist — using $DB_DIR."
            echo "           Merge or remove $LEGACY_DIR manually to silence this."
        fi
    fi
}

MIN_PYTHON="3.10"

# ---- Flags: [--target <name>]... [--engine <name>] [--uninstall] ----
# Reject unknown flags so a typo'd --uninstall can't silently run a full install.
USAGE="Usage: ./install.sh [--target <name>]... [--engine <name>] [--uninstall]"
TARGETS=()
ENGINE="claude-code"
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --target)
            [ -n "${2:-}" ] || { echo "$USAGE"; exit 2; }
            TARGETS+=("$2"); shift 2 ;;
        --engine)
            [ -n "${2:-}" ] || { echo "$USAGE"; exit 2; }
            ENGINE="$2"; shift 2 ;;
        --uninstall)
            UNINSTALL=1; shift ;;
        *)
            echo "$USAGE"
            exit 2 ;;
    esac
done
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=("claude-code")
for t in "${TARGETS[@]}"; do
    case "$t" in
        *[!a-z0-9-]*|"")
            echo "ERROR: invalid target name '$t'."
            exit 2 ;;
    esac
    if [ ! -f "$REPO_DIR/install/targets/$t.sh" ]; then
        echo "ERROR: unknown target '$t' (no install/targets/$t.sh)."
        exit 2
    fi
done
if [ ! -f "$REPO_DIR/install/engines/$ENGINE.sh" ]; then
    echo "ERROR: unknown engine '$ENGINE' (no install/engines/$ENGINE.sh)."
    exit 2
fi

# --uninstall: every target script's uninstall arm runs (hooks, permission,
# skill symlinks — regardless of which targets were selected, so a plain
# --uninstall cleans a multi-target install). The DB and the Python package
# stay — memories are user data.
if [ "$UNINSTALL" -eq 1 ]; then
    echo "ToolEngrams uninstaller (script-install path)"
    for script in "$REPO_DIR"/install/targets/*.sh; do
        bash "$script" uninstall
    done
    command -v engram &>/dev/null && engram consolidate --uninstall-schedule >/dev/null 2>&1 || true
    echo "  Done. Kept: the DB at $DB_DIR (your memories) and the Python package."
    [ -L "$LEGACY_DIR" ] && echo "  (kept the $LEGACY_DIR compatibility symlink — remove it together with the DB)" || true
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

for t in "${TARGETS[@]}"; do
    bash "$REPO_DIR/install/targets/$t.sh" preflight
done
bash "$REPO_DIR/install/engines/$ENGINE.sh" preflight
echo ""

# 1. Install the Python package.
#    Order: uv (if present) -> pip -> pip --user -> dedicated venv. PEP 668
#    "externally-managed-environment" rejects pip AND --user on stock
#    Debian/Ubuntu/Homebrew Python, so the venv fallback is the one that makes
#    a stock machine actually work — engram gets symlinked into ~/.local/bin.
VENV_DIR="$HOME/.local/share/toolengrams/venv"
echo "1. Installing toolengrams package..."
install_ok=0
PATH_WARN=0
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
    # The hooks invoke plain `engram`, so ~/.local/bin must be on PATH. The
    # export below only fixes THIS process (it makes the verification and DB
    # init work) — the user's parent shell still lacks it (stock macOS PATH
    # has no ~/.local/bin), so PATH_WARN makes the closing banner tell them
    # to persist it.
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *)
            export PATH="$HOME/.local/bin:$PATH"
            PATH_WARN=1
            ;;
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

# 2. Per-harness setup, dispatched on the selected targets + engine.
echo "2. Configuring targets (${TARGETS[*]}) and engine ($ENGINE)..."
for t in "${TARGETS[@]}"; do
    bash "$REPO_DIR/install/targets/$t.sh" install
done
bash "$REPO_DIR/install/engines/$ENGINE.sh" install
echo ""

# 3. Initialize DB + verify the wiring. Opening the DB runs the migrations;
#    doctor then re-checks the hook wiring, PATH, claude version, and DB.
echo "3. Initializing database + verifying wiring..."
# Migration MUST precede anything that creates $DB_DIR (including the engine
# persistence below): migrate_legacy_home only moves the legacy home when the
# new one does not exist yet.
migrate_legacy_home
mkdir -p "$DB_DIR"
# Persist the engine choice where launchd/cron's minimal env still finds it
# (engine selection precedence: $ENGRAM_ENGINE -> config.json -> default).
mkdir -p "$DB_DIR"
python3 - "$DB_DIR/config.json" "$ENGINE" << 'PYCONF'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    cfg = json.loads(path.read_text())
except (OSError, ValueError):
    cfg = {}
cfg["engine"] = sys.argv[2]
path.write_text(json.dumps(cfg, indent=2) + "\n")
print(f"  Engine '{sys.argv[2]}' recorded in {path}")
PYCONF
if ! engram doctor; then
    echo ""
    echo "ERROR: 'engram doctor' reported failures above — fix them and re-run this script."
    exit 1
fi
echo "  Database ready at $DB_DIR/db.sqlite"
echo ""

# 4. Optional: install nightly consolidation schedule.
echo "4. Nightly consolidation schedule"
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
if [ "$PATH_WARN" -eq 1 ]; then
    echo "ACTION REQUIRED: engram was linked into ~/.local/bin, which is NOT on"
    echo "your shell's PATH. The install only fixed PATH for this script — add"
    echo "this to your shell profile (~/.zshrc or ~/.bashrc) and restart the shell:"
    echo ""
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    echo "Without it, 'engram' (and the hooks that invoke it) won't be found."
    echo ""
fi
echo "IMPORTANT: hooks load at target session start."
for t in "${TARGETS[@]}"; do
    case "$t" in
        claude-code)
            echo "  - Claude Code: open a NEW Claude Code session." ;;
        codex)
            echo "  - Codex: open a NEW Codex session and trust the ToolEngrams hooks if prompted." ;;
        *)
            echo "  - $t: open a NEW target-agent session." ;;
    esac
done
echo "Sessions already running will not pick them up."
echo ""
echo "Verify it's working (see README, 'Verify it's working'):"
echo "  1. engram seed                       — plant demo memories"
if [[ " ${TARGETS[*]} " == *" claude-code "* ]]; then
    echo "  2. In a NEW Claude Code session, ask Claude to run: ssh deploy@production"
elif [[ " ${TARGETS[*]} " == *" codex "* ]]; then
    echo "  2. In a NEW Codex session, ask Codex to run: ssh deploy@production"
else
    echo "  2. In a NEW target-agent session, ask it to run: ssh deploy@production"
fi
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
echo "Memories form automatically as you use the wired target agent. The first day can be"
echo "quiet — organic memories need real failure→recovery episodes. Watch the"
echo "watcher's decisions live with 'engram monitor'."
