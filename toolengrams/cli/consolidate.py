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

from .. import db
from ..consolidation.agent import run_consolidation_agent
from ..consolidation.collect import collect_sessions


# Session surfaces older than this are cleaned up.
SURFACES_TTL_DAYS = 30


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.install_schedule:
        from ..consolidation.schedule import install_schedule
        path = install_schedule(use_agent=True)
        print(json.dumps({"action": "schedule_installed", "plist_path": path}))
        return 0
    if args.uninstall_schedule:
        from ..consolidation.schedule import uninstall_schedule
        removed = uninstall_schedule()
        print(json.dumps({"action": "schedule_uninstalled", "was_installed": removed}))
        return 0

    target = _resolve_date(args)
    conn = db.connect()

    try:
        # Idempotency.
        if not args.force:
            existing = conn.execute(
                "SELECT id FROM consolidation_runs WHERE run_date = ?",
                (target.isoformat(),),
            ).fetchone()
            if existing:
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

        # Housekeeping: clean old session_surfaces.
        cutoff = int(time.time()) - (SURFACES_TTL_DAYS * 86400)
        cleaned = conn.execute(
            "DELETE FROM session_surfaces WHERE surfaced_ts < ?", (cutoff,)
        ).rowcount

        if args.dry_run:
            print(json.dumps({
                "status": "dry_run",
                "run_date": target.isoformat(),
                "sessions_found": len(sessions),
                "surfaces_would_clean": cleaned,
            }))
            return 0

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
        conn.execute(
            "INSERT OR REPLACE INTO consolidation_runs "
            "(run_date, started_ts, completed_ts, sessions_scanned, "
            "episodes_evaluated, memories_strengthened, memories_weakened, "
            "memories_archived, memories_discovered, report, "
            "quality_score, surfaces_helpful, surfaces_noise) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
            (
                target.isoformat(), now_ts, now_ts, len(sessions),
                metrics.get("surfaces_evaluated", 0),
                metrics.get("memories_pruned", 0),
                metrics.get("memories_created", 0),
                metrics.get("memories_created", 0),
                result.report,
                metrics.get("quality_score"),
                metrics.get("surfaces_helpful", 0),
                metrics.get("surfaces_noise", 0),
            ),
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
    finally:
        conn.close()


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
