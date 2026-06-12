#!/usr/bin/env bash
set -euo pipefail

# ToolEngrams engine script: Claude Code (`claude -p` as the headless runner
# for watcher ticks + consolidation). Called by ../install.sh with one
# argument: preflight | install. No per-engine setup beyond the binary —
# sandbox settings are written per session by engine/claude_code.py.

ACTION="${1:?usage: claude-code.sh preflight|install}"

preflight() {
    if ! command -v claude &>/dev/null; then
        echo "ERROR: the claude-code ENGINE needs the 'claude' CLI on PATH."
        echo "  Install it first: https://claude.com/claude-code"
        exit 1
    fi
    echo "  engine claude-code: claude binary OK"
}

install() {
    :  # nothing to do — selection is persisted by install.sh (config.json)
}

"$ACTION"
