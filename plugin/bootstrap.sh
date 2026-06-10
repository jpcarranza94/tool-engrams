#!/bin/sh
# ToolEngrams plugin venv bootstrap — spawned detached by plugin/hook.sh.
#
# Builds (or rebuilds, after a plugin update) the persistent venv under
# ${CLAUDE_PLUGIN_DATA}, then writes the install stamp hook.sh compares
# against. Serialized by a lock dir so the burst of hooks that all notice a
# missing venv spawns exactly one build.
set -u

ROOT="$1"
DATA="$2"

LOCK="$DATA/bootstrap.lock"
LOG="$DATA/bootstrap.log"
STAMP="$DATA/install.stamp"
# Stale-lock TTL. The package is stdlib-only, so a healthy build is ~1 min;
# a lock this old means a crashed build. The lock mtime is refreshed between
# phases below so a merely-slow build is never mistaken for a dead one.
STALE_LOCK_MIN=15

mkdir -p "$DATA"

# Reap a stale lock from a crashed build, then take the lock.
if [ -d "$LOCK" ]; then
    find "$DATA" -maxdepth 1 -name bootstrap.lock -type d -mmin "+$STALE_LOCK_MIN" \
        -exec rm -rf {} \; 2>/dev/null
fi
mkdir "$LOCK" 2>/dev/null || exit 0   # another build is already running
trap 'rm -rf "$LOCK"' EXIT INT TERM

# Cap the log: every failed build appends, and hooks fire often.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 100000 ]; then
    mv "$LOG" "$LOG.1"
fi

{
    echo "=== bootstrap started: $(date) (root: $ROOT)"
    if ! command -v python3 >/dev/null 2>&1; then
        echo "ERROR: python3 not found on PATH; cannot build the venv."
        exit 1
    fi
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        echo "ERROR: Python >= 3.10 required, found $(python3 --version 2>&1)."
        exit 1
    fi
    # Validate the install source BEFORE destroying the working venv — a
    # vanished plugin root (e.g. the old root after an update) must not
    # leave us venv-less.
    if [ ! -f "$ROOT/pyproject.toml" ]; then
        echo "ERROR: plugin root $ROOT has no pyproject.toml; refusing to rebuild."
        exit 1
    fi
    # --clear so a partial venv from a crashed build can't poison this one.
    if ! python3 -m venv --clear "$DATA/venv"; then
        echo "ERROR: venv creation failed. On Debian/Ubuntu: apt install python3-venv"
        exit 1
    fi
    touch "$LOCK"   # refresh lock mtime so a slow pip phase isn't reaped
    if ! "$DATA/venv/bin/pip" install --quiet "$ROOT"; then
        echo "ERROR: pip install failed — see above."
        exit 1
    fi
    # Skills and the user's own shell call plain `engram`; link it somewhere
    # PATH usually covers. The hooks themselves use the venv path directly.
    mkdir -p "$HOME/.local/bin"
    ln -sf "$DATA/venv/bin/engram" "$HOME/.local/bin/engram"
    # Initialize the DB now so the first live hook doesn't pay migration cost.
    "$DATA/venv/bin/engram" status >/dev/null 2>&1 || true
    # Stamp LAST — a failed build must retry on the next hook.
    { cat "$ROOT/pyproject.toml"; printf '%s' "$ROOT"; } > "$STAMP"
    echo "=== bootstrap done: $(date)"
} >>"$LOG" 2>&1
