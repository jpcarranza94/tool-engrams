#!/bin/sh
# ToolEngrams plugin hook shim — fail-open dispatcher.
#
# Usage: hook.sh <plugin_root> <plugin_data> <engram-subcommand> [args...]
#
# Every plugin hook routes through here. The shim:
#   1. Stamp-compares the installed venv against the current plugin version
#      (pyproject.toml content + plugin root path — the root path changes on
#      every plugin update, so updates trigger a rebuild even when
#      pyproject.toml is unchanged). On mismatch it spawns plugin/bootstrap.sh
#      DETACHED — the build takes tens of seconds and must never run inside a
#      hook's timeout budget.
#   2. Execs the venv engram when present (stdin/stdout pass straight through).
#   3. Fails open with empty hook output while the venv is absent: the memory
#      system is dark for the first session and live from the next one.

ROOT="$1"
DATA="$2"
shift 2

BIN="$DATA/venv/bin/engram"
STAMP="$DATA/install.stamp"

CUR="$(cat "$ROOT/pyproject.toml" 2>/dev/null; printf '%s' "$ROOT")"
OLD="$(cat "$STAMP" 2>/dev/null)"

# Stamp current and binary present: the common path — hand straight off.
# ENGRAM_PLUGIN lets engram (session-start) detect a plugin install and warn
# when legacy script-installed hooks are ALSO present (double-fire).
if [ -x "$BIN" ] && [ "$CUR" = "$OLD" ]; then
    ENGRAM_PLUGIN=1 exec "$BIN" "$@"
fi

# Missing or stale venv: spawn the (lock-serialized) build and fail open.
# Never exec into a venv a concurrent rebuild is clearing — hooks stay dark
# for the build's duration (~1 min), same as the first install.
mkdir -p "$DATA" 2>/dev/null
nohup "$ROOT/plugin/bootstrap.sh" "$ROOT" "$DATA" >/dev/null 2>&1 &

# Venv not ready. Tell the model on SessionStart; stay silent everywhere else.
# A log with errors but no stamp means earlier builds FAILED — say so instead
# of promising "live next session" forever.
if [ "${1:-}" = "session-start" ]; then
    if [ ! -f "$STAMP" ] && grep -q "^ERROR" "$DATA/bootstrap.log" 2>/dev/null; then
        cat <<'EOF'
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "[ToolEngrams] Plugin bootstrap has been FAILING — the memory system is not running. See ~/.claude/plugins/data/*/bootstrap.log for the error (commonly: python3-venv missing)."}}
EOF
    else
        cat <<'EOF'
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "[ToolEngrams] First-run bootstrap in progress: building the plugin venv in the background. The memory system is dark for this session and live from the next one. Check progress: ~/.claude/plugins/data/*/bootstrap.log"}}
EOF
    fi
else
    echo '{}'
fi
exit 0
