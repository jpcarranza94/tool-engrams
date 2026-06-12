"""engram doctor — wiring + liveness diagnostics.

`engram status` reports memory health; doctor reports plumbing health:
are the hooks wired into settings.json, is the `engram` binary reachable,
is Claude Code new enough, is the DB migrated, and has anything actually
fired recently. The liveness signals need no extra writes: every
PostToolUse bumps `session_turns.updated_ts` (so its max is "when did a
hook last fire"), and `watcher_state.last_tick_ts` records watcher runs.

Output: human PASS/WARN/FAIL lines (or --json). Exit 1 when any check
FAILs; WARNs alone exit 0 — a fresh install with no activity yet is
healthy, just quiet.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import db, memory_store, paths, pause
from ..engine import selection as engine_selection
from ..retrieval.session_state import last_activity_ts
from ..watcher import state as watcher_state

MIN_CLAUDE = "2.1.117"  # needs the PostToolUseFailure hook event

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# Hook event -> the exact command install.sh wires for it. Doctor checks the
# same markers the uninstaller keys on, so the three stay in lockstep.
HOOK_MARKERS = {
    "SessionStart": "engram session-start",
    "UserPromptSubmit": "engram user-prompt",
    "PreToolUse": "engram pretool",
    "PostToolUse": "engram post-tool",
    "PostToolUseFailure": "engram post-tool-failure",
    "Stop": "engram stop",
    "SessionEnd": "engram flush",
    "PreCompact": "engram flush",
}

ENGRAM_PERMISSION = "Bash(engram *)"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    checks = run_checks()
    ok = all(c["status"] != FAIL for c in checks)

    if args.json:
        print(json.dumps({"ok": ok, "checks": checks}, indent=2))
    else:
        for c in checks:
            print(f"  {c['status']}  {c['detail']}")
        if not ok:
            print("\nFailures above — re-run ./install.sh or follow the hints, "
                  "then run 'engram doctor' again.")
    return 0 if ok else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engram doctor")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output.")
    return parser


def run_checks() -> list[dict]:
    """All checks, in install order. Each is fully independent so one
    failure (e.g. no settings.json) still lets the rest report."""
    return [
        _check_hooks(),
        _check_permission(),
        _check_engram_on_path(),
        _check_claude_version(),
        _check_engine(),
        _check_home(),
        _check_db(),
        _check_kill_switch(),
        _check_hook_liveness(),
        _check_watcher_liveness(),
    ]


# ---------- individual checks ----------


def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_settings() -> dict | None:
    path = _settings_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _event_has_marker(hooks: dict, event: str, marker: str) -> bool:
    for entry in hooks.get(event, []):
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if cmd == marker or cmd.startswith(marker + " "):
                return True
    return False


def _check_hooks() -> dict:
    settings = _load_settings()
    if settings is None:
        return _check("hooks", FAIL,
                      f"no readable settings at {_settings_path()} — run ./install.sh")
    hooks = settings.get("hooks", {})
    missing = [event for event, marker in HOOK_MARKERS.items()
               if not _event_has_marker(hooks, event, marker)]
    if missing:
        return _check("hooks", FAIL,
                      f"hooks missing for {', '.join(sorted(missing))} — re-run "
                      "./install.sh (doctor checks the user-level settings "
                      "install.sh writes; project-level .claude/settings.json "
                      "wiring is not checked)")
    total = len(HOOK_MARKERS)
    return _check("hooks", PASS,
                  f"hooks wired ({total}/{total} events in {_settings_path()})")


def _check_permission() -> dict:
    settings = _load_settings()
    perms = (settings or {}).get("permissions", {}).get("allow", [])
    if ENGRAM_PERMISSION in perms:
        return _check("permission", PASS, f"{ENGRAM_PERMISSION} permission present")
    return _check("permission", WARN,
                  f"{ENGRAM_PERMISSION} permission missing — engram CLI calls "
                  "will prompt for approval (re-run ./install.sh to add it)")


def _check_engram_on_path() -> dict:
    path = shutil.which("engram")
    if path:
        return _check("engram_path", PASS, f"engram on PATH ({path})")
    return _check("engram_path", FAIL,
                  "engram not on PATH — hooks invoke plain 'engram' and will "
                  "silently no-op. Add the install dir to PATH "
                  "(venv installs: ~/.local/bin)")


def _check_claude_version() -> dict:
    if not shutil.which("claude"):
        return _check("claude", FAIL,
                      "Claude Code CLI ('claude') not found on PATH")
    version = _claude_version()
    if version is None:
        return _check("claude", WARN,
                      "could not parse 'claude --version' output — "
                      f"verify it is >= {MIN_CLAUDE} yourself")
    if _version_tuple(version) < _version_tuple(MIN_CLAUDE):
        return _check("claude", FAIL,
                      f"claude {version} < {MIN_CLAUDE} — the PostToolUseFailure "
                      "hook never fires on this version; update Claude Code")
    return _check("claude", PASS, f"claude {version} (>= {MIN_CLAUDE})")


def _claude_version() -> str | None:
    """Parsed x.y.z from `claude --version`, or None. Isolated for tests."""
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\d+\.\d+\.\d+", out or "")
    return match.group(0) if match else None


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split(".")[:3])


def _check_engine() -> dict:
    """The configured engine must exist in the registry and have its binary on
    PATH. Detached ticks swallow selection's stderr fallback warning, so this
    is where an `ENGRAM_ENGINE` typo actually surfaces."""
    name = engine_selection.configured_engine_name()
    engine = engine_selection.ENGINES.get(name)
    if engine is None:
        return _check("engine", FAIL,
                      f"configured engine {name!r} is unknown "
                      f"(known: {', '.join(sorted(engine_selection.ENGINES))}) — "
                      "background work silently falls back to claude-code")
    if not engine.is_available():
        return _check("engine", FAIL,
                      f"engine {name}: binary not found on PATH — "
                      "watcher ticks and consolidation cannot run")
    return _check("engine", PASS, f"engine: {name} (binary on PATH)")


def _check_home() -> dict:
    home = paths.engram_home()
    if home == paths.LEGACY_HOME:
        return _check("home", WARN,
                      f"data home: {home} (legacy location — re-run "
                      f"./install.sh to migrate to {paths.DEFAULT_HOME})")
    # Split-brain: resolution picked `home`, but a real (non-symlink) legacy
    # dir still exists — old package versions write there, new ones here.
    if paths.LEGACY_HOME.is_dir() and not paths.LEGACY_HOME.is_symlink():
        return _check("home", WARN,
                      f"data home: {home}, but {paths.LEGACY_HOME} also exists "
                      "— old engram versions still write there; merge or "
                      "remove it (or re-run ./install.sh)")
    return _check("home", PASS, f"data home: {home}")


def _check_db() -> dict:
    # Opening the connection creates the DB and runs migrations on first
    # touch — install.sh step 4 relies on exactly that side effect.
    try:
        with db.session() as conn:
            schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
            active = memory_store.health_stats(conn)["active"]
    except Exception as e:
        return _check("db", FAIL, f"cannot open DB at {db.db_path()}: {e}")
    return _check("db", PASS,
                  f"db ok (schema v{schema_version}, {active} active memories, "
                  f"{db.db_path()})")


def _check_kill_switch() -> dict:
    if not pause.is_disabled():
        return _check("kill_switch", PASS, "kill switch off — system active")
    return _check("kill_switch", WARN,
                  "system is PAUSED (engram pause flag or ENGRAM_DISABLED) — "
                  "no surfacing or ticks until 'engram resume'")


def _check_hook_liveness() -> dict:
    try:
        with db.session() as conn:
            last_ts = last_activity_ts(conn)
    except Exception as e:
        return _check("hook_liveness", WARN, f"could not read activity: {e}")
    if last_ts <= 0:
        return _check("hook_liveness", WARN,
                      "no hook activity recorded yet — hooks load at session "
                      "start, so open a NEW Claude Code session and run any "
                      "tool call, then re-check")
    return _check("hook_liveness", PASS,
                  f"hooks alive — last tool-call hook fired {_ago(last_ts)}")


def _check_watcher_liveness() -> dict:
    try:
        last_ts = watcher_state.last_tick_ts_any()
    except Exception as e:
        return _check("watcher_liveness", WARN, f"could not read watcher state: {e}")
    if last_ts <= 0:
        return _check("watcher_liveness", WARN,
                      "watcher has never ticked — expected on a fresh install; "
                      "it fires after completed turns in a real session")
    return _check("watcher_liveness", PASS,
                  f"watcher alive — last tick {_ago(last_ts)}")


# ---------- helpers ----------


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _ago(ts: int) -> str:
    return _format_ago(max(0, int(time.time()) - ts))


def _format_ago(delta: int) -> str:
    if delta < 90:
        return f"{delta}s ago"
    if delta < 90 * 60:
        return f"{delta // 60} min ago"
    if delta < 36 * 3600:
        return f"{delta // 3600} h ago"
    return f"{delta // 86400} d ago"
