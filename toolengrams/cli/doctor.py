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
import shutil
import sqlite3
import time
from pathlib import Path

from .. import db, memory_store, paths, pause
from ..engine import selection as engine_selection
from ..target import TARGETS
from ..retrieval.session_state import last_activity_ts
from ..watcher import state as watcher_state

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# Hook event -> the command marker install.sh wires for it — owned by the
# target adapter so doctor, installer, and uninstaller stay in lockstep.
HOOK_MARKERS = TARGETS["claude-code"].hook_markers()

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
        *_check_target_versions(),
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


def _check_hooks() -> dict:
    statuses = [_target_hook_status(target) for target in TARGETS.values()]
    partial = [s for s in statuses if s["seen"] and s["missing"]]
    if partial:
        detail = "; ".join(
            f"{s['name']} missing {', '.join(sorted(s['missing']))}"
            for s in partial
        )
        return _check("hooks", FAIL, f"partial target wiring: {detail}")

    wired = [s for s in statuses if s["seen"] and not s["missing"]]
    if wired:
        detail = ", ".join(
            f"{s['name']} ({s['total']}/{s['total']} events)"
            for s in wired
        )
        return _check("hooks", PASS, f"target hooks wired: {detail}")

    return _check("hooks", FAIL,
                  "no target hooks wired — run ./install.sh")


def _target_hook_status(target) -> dict:
    status = dict(target.hook_status())
    status["name"] = target.NAME
    return status


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


def _check_target_versions() -> list[dict]:
    checks = []
    for target in TARGETS.values():
        if _target_hook_status(target)["seen"]:
            checks.append(_check_target_version(target))
    return checks


def _check_target_version(target) -> dict:
    status = _target_hook_status(target)
    if not status["seen"]:
        return _check(target.NAME, WARN, f"{target.NAME} target hooks not wired")
    if status["missing"]:
        return _check(
            target.NAME,
            FAIL,
            f"{target.NAME} target hooks incomplete: missing "
            f"{', '.join(sorted(status['missing']))}",
        )
    if not shutil.which(target.cli_binary):
        return _check(target.NAME, FAIL,
                      f"{target.NAME} CLI ('{target.cli_binary}') not found on PATH")
    version = target.installed_version()
    if version is None:
        return _check(target.NAME, WARN,
                      f"could not parse '{target.cli_binary} --version' output — "
                      f"verify it is >= {target.min_version} yourself")
    if _version_tuple(version) < _version_tuple(target.min_version):
        return _check(target.NAME, FAIL,
                      f"{target.cli_binary} {version} < {target.min_version} — "
                      f"update {target.NAME}")
    return _check(target.NAME, PASS,
                  f"{target.cli_binary} {version} (>= {target.min_version})")


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
    minimum = getattr(engine, "min_version", None)
    if minimum:
        version = engine.installed_version()
        if version is None:
            return _check("engine", WARN,
                          f"engine {name}: could not parse version — verify it "
                          f"is >= {minimum} yourself")
        if _version_tuple(version) < _version_tuple(minimum):
            return _check("engine", FAIL,
                          f"engine {name}: {version} < {minimum} — update the "
                          "CLI before watcher ticks or consolidation run")
        return _check("engine", PASS,
                      f"engine: {name} {version} (>= {minimum})")
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
            drift = _schema_drift(conn, schema_version)
    except Exception as e:
        return _check("db", FAIL, f"cannot open DB at {db.db_path()}: {e}")
    if drift:
        return _check("db", FAIL,
                      f"schema drift at v{schema_version}: missing {drift} — the "
                      "version was stamped without its migration running. Restore "
                      "from a backup or re-apply the migration by hand; "
                      "PRAGMA user_version only proves the number, not the columns")
    return _check("db", PASS,
                  f"db ok (schema v{schema_version}, {active} active memories, "
                  f"{db.db_path()})")


def _columns_by_table(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Map each table name to its column set (index access works for both Row
    and plain-tuple row factories)."""
    out: dict[str, set[str]] = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        name = row[0]
        out[name] = {c[1] for c in conn.execute(f"PRAGMA table_info({name})")}
    return out


def _snapshot_columns() -> dict[str, set[str]]:
    """Columns per table from the canonical schema.sql snapshot, built in an
    in-memory DB so doctor compares against the code, not a hand-kept manifest."""
    snap = sqlite3.connect(":memory:")
    try:
        snap.executescript(db.SCHEMA_PATH.read_text())
        return _columns_by_table(snap)
    finally:
        snap.close()


def _schema_drift(conn: sqlite3.Connection, version: int) -> str | None:
    """Expected-but-missing 'table.column's, or None if the live schema matches.

    schema.sql is a v_latest snapshot, so the comparison is only meaningful when
    the DB is stamped at the latest version. A mismatch there means user_version
    was bumped without the migration that adds the column actually running — the
    silent-skip failure mode _apply_migrations now guards against; this is the
    at-rest detector for a DB already in that state. Only missing columns are
    flagged (extra columns from a newer DB are ignored)."""
    if version != db.SCHEMA_VERSION:
        return None
    actual = _columns_by_table(conn)
    missing = [
        f"{table}.{col}"
        for table, cols in _snapshot_columns().items()
        for col in cols
        if col not in actual.get(table, set())
    ]
    return ", ".join(sorted(missing)) if missing else None


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
                      "no hook activity recorded yet — hooks load at target "
                      "session start, so open a NEW target-agent session and "
                      "run any tool call, then re-check")
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
