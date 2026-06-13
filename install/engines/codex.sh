#!/usr/bin/env bash
set -euo pipefail

# ToolEngrams engine script: Codex (`codex exec` as the headless runner for
# watcher ticks + consolidation). Called by ../install.sh with one argument:
# preflight | install. Auth is owned by Codex itself (~/.codex/auth.json,
# CODEX_API_KEY, or OPENAI_API_KEY); the user should run `codex login`.

ACTION="${1:?usage: codex.sh preflight|install}"

preflight() {
    if ! command -v codex &>/dev/null; then
        echo "ERROR: the codex ENGINE needs the 'codex' CLI on PATH."
        echo "  Install Codex first, then authenticate with: codex login"
        exit 1
    fi
    echo "  engine codex: codex binary OK"
    echo "  engine codex: auth is handled by Codex (run 'codex login' if needed)"
}

install() {
    :  # nothing to do — selection is persisted by install.sh (config.json)
}

"$ACTION"
