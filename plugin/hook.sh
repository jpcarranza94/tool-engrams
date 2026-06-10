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
if [ -x "$BIN" ] && [ "$CUR" = "$OLD" ]; then
    exec "$BIN" "$@"
fi

# Missing or stale venv: spawn the (lock-serialized) build and fail open.
# Never exec into a venv a concurrent rebuild is clearing — hooks stay dark
# for the build's duration (~1 min), same as the first install.
mkdir -p "$DATA" 2>/dev/null
nohup "$ROOT/plugin/bootstrap.sh" "$ROOT" "$DATA" >/dev/null 2>&1 &

# Venv not ready. Tell the model on SessionStart; stay silent everywhere else.
if [ "${1:-}" = "session-start" ]; then
    cat <<'EOF'
{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "[ToolEngrams] First-run bootstrap in progress: building the plugin venv in the background. The memory system is dark for this session and live from the next one. Check progress: ~/.claude/plugins/data/*/bootstrap.log"}}
EOF
else
    echo '{}'
fi
exit 0
