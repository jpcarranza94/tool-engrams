#!/usr/bin/env bash
set -euo pipefail

# ToolEngrams engine script: Codex (`codex exec` as the headless runner for
# watcher ticks + consolidation). Called by ../install.sh with one argument:
# preflight | install. Auth is owned by Codex itself (~/.codex/auth.json,
# CODEX_API_KEY, or OPENAI_API_KEY); the user should run `codex login`.

ACTION="${1:?usage: codex.sh preflight|install}"
MIN_CODEX="0.137.0"

codex_version() {
    codex --version 2>&1 | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true
}

version_ge() {
    local have="$1" need="$2"
    local IFS=.
    local -a h n
    read -r -a h <<< "$have"
    read -r -a n <<< "$need"
    for i in 0 1 2; do
        local hv="${h[$i]:-0}"
        local nv="${n[$i]:-0}"
        if (( hv > nv )); then return 0; fi
        if (( hv < nv )); then return 1; fi
    done
    return 0
}

preflight() {
    if ! command -v codex &>/dev/null; then
        echo "ERROR: the codex ENGINE needs the 'codex' CLI on PATH."
        echo "  Install Codex first, then authenticate with: codex login"
        exit 1
    fi
    local version
    version="$(codex_version)"
    if [ -z "$version" ]; then
        echo "ERROR: could not parse 'codex --version' output."
        echo "  ToolEngrams codex ENGINE requires codex >= $MIN_CODEX."
        exit 1
    fi
    if ! version_ge "$version" "$MIN_CODEX"; then
        echo "ERROR: codex $version is too old for the ToolEngrams ENGINE."
        echo "  Update Codex to >= $MIN_CODEX, then authenticate with: codex login"
        exit 1
    fi
    echo "  engine codex: codex $version OK"
    echo "  engine codex: auth is handled by Codex (run 'codex login' if needed)"
}

install() {
    :  # nothing to do — selection is persisted by install.sh (config.json)
}

"$ACTION"
