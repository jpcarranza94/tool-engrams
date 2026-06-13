#!/usr/bin/env bash
set -euo pipefail

# ToolEngrams target script: Codex hooks.
# Called by ../install.sh with one argument: preflight | install | uninstall.

MIN_CODEX="0.137.0"
ACTION="${1:?usage: codex.sh preflight|install|uninstall}"
CODEX_CONFIG="${CODEX_CONFIG:-$HOME/.codex/config.toml}"
CODEX_HOOKS="${CODEX_HOOKS:-$HOME/.codex/hooks.json}"

preflight() {
    if ! command -v codex &>/dev/null; then
        echo "ERROR: Codex CLI ('codex') not found on PATH."
        echo "  Install Codex, run 'codex login', then re-run this script."
        exit 1
    fi
    CODEX_VERSION="$(codex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
    if [ -z "$CODEX_VERSION" ]; then
        echo "WARNING: could not parse 'codex --version' output; continuing."
    elif ! python3 -c 'import sys
have = [int(x) for x in sys.argv[1].split(".")[:3]]
need = [int(x) for x in sys.argv[2].split(".")[:3]]
sys.exit(0 if have >= need else 1)' "$CODEX_VERSION" "$MIN_CODEX"; then
        echo "ERROR: codex >= $MIN_CODEX required for hooks, found $CODEX_VERSION."
        exit 1
    else
        echo "  codex $CODEX_VERSION OK"
    fi
}

install() {
    echo "  Configuring Codex hooks..."
    mkdir -p "$(dirname "$CODEX_CONFIG")" "$(dirname "$CODEX_HOOKS")"

    python3 - "$CODEX_CONFIG" "$CODEX_HOOKS" << 'PYEOF'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
hooks_path = Path(sys.argv[2])

text = config_path.read_text() if config_path.exists() else ""
lines = text.splitlines()
out = []
in_features = False
features_seen = False
hooks_set = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if in_features and not hooks_set:
            out.append("hooks = true")
            hooks_set = True
        in_features = stripped == "[features]"
        features_seen = features_seen or in_features
        out.append(line)
        continue
    if in_features and stripped.startswith("hooks"):
        out.append("hooks = true")
        hooks_set = True
        continue
    out.append(line)
if in_features and not hooks_set:
    out.append("hooks = true")
if not features_seen:
    if out and out[-1].strip():
        out.append("")
    out.extend(["[features]", "hooks = true"])
config_path.write_text("\n".join(out).rstrip() + "\n")
print("  Enabled [features] hooks = true")

try:
    data = json.loads(hooks_path.read_text())
except (OSError, ValueError):
    data = {}
hooks = data.setdefault("hooks", {})

tool_matcher = "Bash|apply_patch"
wiring = [
    ("SessionStart", "engram session-start", "engram session-start --target codex", "", 5000),
    ("PreToolUse", "engram pretool", "engram pretool --target codex", tool_matcher, 3000),
    ("PostToolUse", "engram post-tool", "engram post-tool --target codex", tool_matcher, 3000),
    ("UserPromptSubmit", "engram user-prompt", "engram user-prompt --target codex", "", 2000),
    ("Stop", "engram stop", "engram stop --target codex", "", 5000),
    ("PreCompact", "engram flush", "engram flush --target codex", "", 5000),
]
for event, marker, command, matcher, timeout in wiring:
    present = any(
        h.get("command", "") == marker or h.get("command", "").startswith(marker + " ")
        for entry in hooks.get(event, [])
        for h in entry.get("hooks", [])
    )
    if present:
        print(f"  {event} hook already present")
        continue
    hooks.setdefault(event, []).append({
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": timeout}],
    })
    print(f"  Added {event} hook")

hooks_path.write_text(json.dumps(data, indent=2) + "\n")
print("  Hooks saved")
PYEOF

    echo ""
    echo "  IMPORTANT: Codex uses hash-based hook trust."
    echo "  On the first Codex run after this install, approve/trust the new"
    echo "  ToolEngrams hooks. Editing hooks.json later will prompt again."
}

uninstall() {
    if [ -f "$CODEX_HOOKS" ]; then
        cp -p "$CODEX_HOOKS" "$CODEX_HOOKS.uninstall.bak"
        python3 - "$CODEX_HOOKS" << 'PYEOF'
import json
import sys
from pathlib import Path

hooks_path = Path(sys.argv[1])
data = json.loads(hooks_path.read_text())
hooks = data.get("hooks", {})
removed = 0
for event in list(hooks):
    kept_entries = []
    for entry in hooks[event]:
        entry_hooks = entry.get("hooks", [])
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
hooks_path.write_text(json.dumps(data, indent=2) + "\n")
print(f"  Removed {removed} engram hooks from hooks.json")
PYEOF
    fi
}

"$ACTION"
