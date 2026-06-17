"""Nightly consolidation: `engram consolidate` — sleep for memories.

Spawns an Opus agent that freely explores today's sessions, evaluates
memory surfacing quality, identifies missed corrections, and takes
action via the engram CLI. This is the "sleep consolidation" — the
brain replaying the day's experiences.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta

from .. import db, pause
from ..consolidation import runs
from ..consolidation.agent import run_consolidation_agent
from ..consolidation.schedule import install_schedule, uninstall_schedule
from ..retrieval import session_state
from ..target import TARGETS, SessionFile
from ..utils import env_int
from ..watcher import runs_store
from .. import envvars


# Session surfaces older than this are cleaned up.
SURFACES_TTL_DAYS = 30
# Watcher run-log rows (monitor history) older than this are cleaned up.
WATCHER_RUNS_TTL_DAYS = 14

# Catch-up window for the scheduled run. `--yesterday` scans this many days
# back (through yesterday) and consolidates every day that has sessions but no
# recorded run. This makes consolidation gap-driven instead of "yesterday from
# now": a day whose run was missed (laptop off when the 8 AM job would fire) is
# backfilled on the next run. Days older than the window are dropped — bounding
# both cost and the rescan of empty days. See docs/adr/0011.
CATCHUP_LOOKBACK_DAYS = 7


def collect_sessions(target_date: date) -> list[SessionFile]:
    """Collect sessions from every wired target, tagged by adapter name."""
    sessions: list[SessionFile] = []
    for target in TARGETS.values():
        if not target.is_wired():
            continue
        try:
            target_sessions = target.collect_sessions(target_date)
        except Exception as e:
            print(
                f"engram consolidate: target {target.NAME} collection failed: {e}",
                file=sys.stderr,
            )
            continue
        sessions.extend(replace(session, target=target.NAME)
                        for session in target_sessions)
    sessions.sort(key=lambda s: s.modified_ts)
    return sessions


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.install_schedule:
        path = install_schedule()
        print(json.dumps({"action": "schedule_installed", "plist_path": path}))
        return 0
    if args.uninstall_schedule:
        removed = uninstall_schedule()
        print(json.dumps({"action": "schedule_uninstalled", "was_installed": removed}))
        return 0

    # Kill switch: the scheduled nightly run is the most expensive single
    # model call in the system — a paused system must not spend. Schedule
    # install/uninstall above stays usable while paused.
    if pause.is_disabled():
        print(json.dumps({"action": "skipped", "reason": "paused"}))
        return 0

    targets = _resolve_dates(args)

    with db.session() as conn:
        if args.dry_run:
            _print_dry_run(conn, targets)
            return 0

        # Single-sweep lock: RunAtLoad (boot) + the 8 AM StartCalendarInterval
        # can fire two `engram consolidate` processes that overlap (a multi-day
        # sweep runs for minutes per day), and a 30-min agent call sits between
        # was_run() and record_run() — so two sweeps could both spawn an Opus
        # agent for the same day. Non-blocking: a second concurrent sweep exits
        # cleanly rather than double-spending.
        with _consolidate_lock() as acquired:
            if not acquired:
                print(json.dumps({"action": "skipped", "reason": "already_running"}))
                return 0

            # Housekeeping once per invocation (not once per backfilled day):
            # prune old session_surfaces + watcher run-log rows.
            now_ts = int(time.time())
            cleaned = session_state.prune_surfaces_before(
                conn, now_ts - env_int(envvars.SURFACES_TTL_DAYS, SURFACES_TTL_DAYS) * 86400)
            runs_store.prune_runs_before(
                conn, now_ts - env_int(envvars.WATCHER_RUNS_TTL_DAYS, WATCHER_RUNS_TTL_DAYS) * 86400)

            # Consolidate each candidate day, oldest first, so a later day's
            # surfacing evaluation sees the memory state earlier days left behind.
            results = [_consolidate_date(conn, target, force=args.force)
                       for target in targets]

            return _print_results(results, cleaned, json_out=args.json)


@contextmanager
def _consolidate_lock():
    """Non-blocking process lock so overlapping fires don't double-spend Opus
    calls on the same day. Yields True if acquired, False if another sweep holds
    it. The lockfile lives next to the DB (honors $ENGRAM_DB in tests)."""
    lock_dir = db.db_path().parent / "locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        yield True  # can't create the lock dir → don't block the only sweep
        return
    f = open(lock_dir / "consolidate.lock", "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def _consolidate_date(conn, target: date, *, force: bool) -> dict:
    """Consolidate one day. Returns a result dict (no I/O beyond stderr).

    Skips a day already recorded (idempotency; --force bypasses). A day with no
    sessions is a no-op — deliberately NOT recorded, so it costs only a cheap
    disk glob on each scan and never pollutes the run history. A day whose agent
    errors is also NOT recorded, so a transient failure (spawn/timeout/PATH) is
    retried on the next run instead of being permanently skipped.
    """
    iso = target.isoformat()

    if not force and runs.was_run(conn, iso):
        return {"status": "already_run", "run_date": iso}

    sessions = collect_sessions(target)
    if not sessions:
        return {"status": "no_sessions", "run_date": iso}

    result = run_consolidation_agent(
        sessions=sessions,
        db_path=db.db_path(),
        target_date=iso,
    )

    if result.error:
        # Do not record: leave the day un-run so the next catch-up retries it.
        print(f"engram consolidate: {iso}: agent error: {result.error}",
              file=sys.stderr)
        return {"status": "error", "run_date": iso,
                "sessions_scanned": len(sessions), "error": result.error,
                "report": result.report}

    # Parse structured metrics from the report (JSON block at the end).
    metrics = _extract_metrics(result.report or "")

    now_ts = int(time.time())
    runs.record_run(
        conn,
        run_date=iso,
        started_ts=now_ts,
        completed_ts=now_ts,
        sessions_scanned=len(sessions),
        episodes_evaluated=metrics.get("surfaces_evaluated", 0),
        memories_weakened=metrics.get("memories_pruned", 0),
        # NOTE: archived + discovered both map to memories_created (preserved
        # from the original recorder — the agent reports one "created" count).
        memories_archived=metrics.get("memories_created", 0),
        memories_discovered=metrics.get("memories_created", 0),
        report=result.report,
        quality_score=metrics.get("quality_score"),
        surfaces_helpful=metrics.get("surfaces_helpful", 0),
        surfaces_noise=metrics.get("surfaces_noise", 0),
        memories_verified=metrics.get("memories_verified", 0),
    )

    return {"status": "completed", "run_date": iso,
            "sessions_scanned": len(sessions), "error": None,
            "report": result.report}


def _print_dry_run(conn, targets: list[date]) -> None:
    """Report the catch-up plan without spawning agents or mutating state."""
    now_ts = int(time.time())
    plan = []
    for target in targets:
        iso = target.isoformat()
        if runs.was_run(conn, iso):
            plan.append({"run_date": iso, "action": "skip_already_run"})
            continue
        found = len(collect_sessions(target))
        plan.append({
            "run_date": iso,
            "action": "consolidate" if found else "skip_no_sessions",
            "sessions_found": found,
        })
    print(json.dumps({
        "status": "dry_run",
        "plan": plan,
        "surfaces_would_clean": session_state.count_surfaces_before(
            conn, now_ts - env_int(envvars.SURFACES_TTL_DAYS, SURFACES_TTL_DAYS) * 86400),
        "watcher_runs_would_clean": runs_store.count_runs_before(
            conn, now_ts - env_int(envvars.WATCHER_RUNS_TTL_DAYS, WATCHER_RUNS_TTL_DAYS) * 86400),
    }))


def _print_results(results: list[dict], cleaned: int, *, json_out: bool) -> int:
    """Render per-day results; return process exit code (1 if any day errored)."""
    any_error = any(r["status"] == "error" for r in results)

    if json_out:
        print(json.dumps({
            "status": "error" if any_error else "completed",
            "surfaces_cleaned": cleaned,
            "runs": [{k: v for k, v in r.items() if k != "report"}
                     for r in results],
        }))
    else:
        reported = False
        for r in results:
            if r.get("report"):
                print(r["report"])
                reported = True
        if not reported:
            # No agent ran (all skipped/empty) — surface why instead of silence.
            print(json.dumps([{k: v for k, v in r.items() if k != "report"}
                              for r in results]))

    return 1 if any_error else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram consolidate")
    parser.add_argument("--yesterday", action="store_true",
                        help=f"Catch-up sweep: consolidate every un-run day in "
                             f"the last {CATCHUP_LOOKBACK_DAYS} days through "
                             f"yesterday (for scheduled runs).")
    parser.add_argument("--date", default=None,
                        help="Specific date (YYYY-MM-DD).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen; don't spawn agent.")
    parser.add_argument("--force", action="store_true",
                        help="Re-run even if already consolidated. With "
                             "--yesterday this re-runs the WHOLE catch-up window "
                             "(up to 7 agent calls) — use --date to re-run one day.")
    parser.add_argument("--json", action="store_true",
                        help="JSON output.")
    parser.add_argument("--install-schedule", action="store_true",
                        help="Install scheduled daily consolidation (launchd on macOS, cron on Linux, 8 AM).")
    parser.add_argument("--uninstall-schedule", action="store_true",
                        help="Remove the launchd plist.")
    return parser.parse_args(argv)


def _extract_metrics(report: str) -> dict:
    """Parse the structured JSON metrics block from the consolidation report.

    The agent is instructed to end its report with a ```json block
    containing a "metrics" key. We try to extract it; if parsing fails
    we return an empty dict (graceful degradation — the report text
    is still stored either way).
    """
    try:
        # Find the last JSON block in the report.
        last_brace = report.rfind("}")
        if last_brace < 0:
            return {}
        # Walk backwards to find the matching opening brace.
        depth = 0
        for i in range(last_brace, -1, -1):
            if report[i] == "}":
                depth += 1
            elif report[i] == "{":
                depth -= 1
            if depth == 0:
                candidate = report[i:last_brace + 1]
                parsed = json.loads(candidate)
                return parsed.get("metrics", parsed)
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


def _resolve_dates(args: argparse.Namespace) -> list[date]:
    """Dates to consolidate, oldest first.

    --date D    → that single day (explicit override / manual backfill).
    --yesterday → catch-up window [today - LOOKBACK … yesterday]; the scheduled
                  job uses this so days missed while the laptop was off get
                  backfilled on the next run. was_run() skips already-done days.
    (none)      → today (manual current-day run).
    """
    if args.date:
        return [date.fromisoformat(args.date)]
    today = date.today()
    if args.yesterday:
        lookback = env_int(envvars.CATCHUP_LOOKBACK_DAYS, CATCHUP_LOOKBACK_DAYS)
        return [today - timedelta(days=n)
                for n in range(lookback, 0, -1)]
    return [today]
