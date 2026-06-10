"""Nightly consolidation: `engram consolidate` — sleep for memories.

Spawns an Opus agent that freely explores today's sessions, evaluates
memory surfacing quality, identifies missed corrections, and takes
action via the engram CLI. This is the "sleep consolidation" — the
brain replaying the day's experiences.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta

from .. import db, pause
from ..consolidation import runs
from ..consolidation.agent import run_consolidation_agent
from ..consolidation.collect import collect_sessions
from ..consolidation.schedule import install_schedule, uninstall_schedule
from ..retrieval import session_state
from ..watcher import runs_store


# Session surfaces older than this are cleaned up.
SURFACES_TTL_DAYS = 30
# Watcher run-log rows (monitor history) older than this are cleaned up.
WATCHER_RUNS_TTL_DAYS = 14


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

    target = _resolve_date(args)

    with db.session() as conn:
        # Idempotency.
        if not args.force:
            if runs.was_run(conn, target.isoformat()):
                print(json.dumps({
                    "status": "already_run",
                    "run_date": target.isoformat(),
                    "message": "Already consolidated. Use --force to re-run.",
                }))
                return 0

        sessions = collect_sessions(target)
        if not sessions:
            print(json.dumps({"status": "no_sessions", "run_date": target.isoformat()}))
            return 0

        # Housekeeping window for old session_surfaces + watcher run-log rows.
        now_ts = int(time.time())
        surfaces_cutoff = now_ts - SURFACES_TTL_DAYS * 86400
        runs_cutoff = now_ts - WATCHER_RUNS_TTL_DAYS * 86400

        if args.dry_run:
            # Dry-run must not mutate: report what WOULD be pruned.
            print(json.dumps({
                "status": "dry_run",
                "run_date": target.isoformat(),
                "sessions_found": len(sessions),
                "surfaces_would_clean": session_state.count_surfaces_before(conn, surfaces_cutoff),
                "watcher_runs_would_clean": runs_store.count_runs_before(conn, runs_cutoff),
            }))
            return 0

        # Real run: prune, then consolidate.
        cleaned = session_state.prune_surfaces_before(conn, surfaces_cutoff)
        runs_store.prune_runs_before(conn, runs_cutoff)

        # Run the consolidation agent.
        result = run_consolidation_agent(
            sessions=sessions,
            db_path=db.db_path(),
            target_date=target.isoformat(),
        )

        if result.error:
            print(f"engram consolidate: agent error: {result.error}", file=sys.stderr)

        # Parse structured metrics from the report (JSON block at the end).
        metrics = _extract_metrics(result.report or "")

        # Log the run.
        now_ts = int(time.time())
        runs.record_run(
            conn,
            run_date=target.isoformat(),
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

        if args.json:
            print(json.dumps({
                "status": "completed" if not result.error else "error",
                "run_date": target.isoformat(),
                "sessions_scanned": len(sessions),
                "surfaces_cleaned": cleaned,
                "error": result.error,
            }))
        else:
            print(result.report or "Agent produced no report.")

        return 0 if not result.error else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram consolidate")
    parser.add_argument("--yesterday", action="store_true",
                        help="Consolidate yesterday (for scheduled runs).")
    parser.add_argument("--date", default=None,
                        help="Specific date (YYYY-MM-DD).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen; don't spawn agent.")
    parser.add_argument("--force", action="store_true",
                        help="Re-run even if already consolidated.")
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


def _resolve_date(args: argparse.Namespace) -> date:
    if args.date:
        return date.fromisoformat(args.date)
    if args.yesterday:
        return date.today() - timedelta(days=1)
    return date.today()
