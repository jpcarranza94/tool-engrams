"""Nightly consolidation: `engram consolidate` — sleep for memories.

Two modes:
  1. Mechanical (default): auto-archive dead memories, flag stale, clean up surfaces.
  2. Agent (--agent): spawn an Opus agent that freely explores today's sessions,
     evaluates memory quality, identifies missed corrections, and takes action.

The agent mode is the "sleep consolidation" — the brain replaying the day's
experiences to decide what to keep and what to let go.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

from .. import db
from ..consolidation.adjust import AdjustmentReport, run_mechanical_adjustments
from ..consolidation.collect import collect_sessions


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Schedule management.
    if args.install_schedule:
        from ..schedule import install_schedule
        path = install_schedule(use_agent=args.agent)
        print(json.dumps({"action": "schedule_installed", "plist_path": path}))
        return 0
    if args.uninstall_schedule:
        from ..schedule import uninstall_schedule
        removed = uninstall_schedule()
        print(json.dumps({"action": "schedule_uninstalled", "was_installed": removed}))
        return 0

    target = _resolve_date(args)

    conn = db.connect()
    try:
        # Idempotency check.
        if not args.force:
            existing = conn.execute(
                "SELECT id FROM consolidation_runs WHERE run_date = ?",
                (target.isoformat(),),
            ).fetchone()
            if existing:
                print(json.dumps({
                    "status": "already_run",
                    "run_date": target.isoformat(),
                    "message": "Consolidation already ran for this date. Use --force to re-run.",
                }))
                return 0

        # Collect sessions.
        sessions = collect_sessions(target)

        # Mechanical adjustments (always, unless dry-run).
        if args.dry_run:
            adjustment = AdjustmentReport()
        else:
            adjustment = run_mechanical_adjustments(conn)

        # Agent-based review (optional).
        agent_report = None
        if args.agent and sessions and not args.dry_run:
            from ..consolidation.agent import run_consolidation_agent
            result = run_consolidation_agent(
                sessions=sessions,
                db_path=db.db_path(),
                target_date=target.isoformat(),
            )
            if result.error:
                print(f"engram consolidate: agent error: {result.error}", file=sys.stderr)
            agent_report = result.report or None

        # Build report.
        report_lines = [
            f"ToolEngrams consolidation — {target.isoformat()}",
            "=" * 50,
            f"Sessions scanned: {len(sessions)}",
            f"Archived (dead): {len(adjustment.archived_ids)}",
        ]
        for name in adjustment.archived_names:
            report_lines.append(f"  - {name}")
        report_lines.append(f"Flagged stale: {len(adjustment.stale_ids)}")
        for name in adjustment.stale_names:
            report_lines.append(f"  - {name}")
        report_lines.append(f"Session surfaces cleaned: {adjustment.surfaces_cleaned}")

        if agent_report:
            report_lines.append("")
            report_lines.append("Agent review:")
            report_lines.append("-" * 40)
            report_lines.append(agent_report)

        report_text = "\n".join(report_lines)

        # Log the run.
        if not args.dry_run:
            now_ts = int(time.time())
            conn.execute(
                "INSERT OR REPLACE INTO consolidation_runs "
                "(run_date, started_ts, completed_ts, sessions_scanned, "
                "episodes_evaluated, memories_strengthened, memories_weakened, "
                "memories_archived, memories_discovered, report) "
                "VALUES (?, ?, ?, ?, 0, 0, 0, ?, 0, ?)",
                (
                    target.isoformat(),
                    now_ts, now_ts,
                    len(sessions),
                    len(adjustment.archived_ids),
                    report_text,
                ),
            )

        if args.json:
            print(json.dumps({
                "status": "completed",
                "run_date": target.isoformat(),
                "sessions_scanned": len(sessions),
                "archived": len(adjustment.archived_ids),
                "stale": len(adjustment.stale_ids),
                "surfaces_cleaned": adjustment.surfaces_cleaned,
                "agent_ran": agent_report is not None,
            }))
        else:
            print(report_text)

        return 0
    finally:
        conn.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram consolidate")
    parser.add_argument("--yesterday", action="store_true",
                        help="Consolidate yesterday (default for scheduled runs).")
    parser.add_argument("--date", default=None,
                        help="Specific date to consolidate (YYYY-MM-DD).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; don't modify DB or spawn agent.")
    parser.add_argument("--force", action="store_true",
                        help="Re-run even if already consolidated for this date.")
    parser.add_argument("--agent", action="store_true",
                        help="Spawn an Opus agent to review sessions and evaluate memories.")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON instead of text report.")
    parser.add_argument("--install-schedule", action="store_true",
                        help="Install macOS launchd plist for nightly 2 AM runs.")
    parser.add_argument("--uninstall-schedule", action="store_true",
                        help="Remove the launchd plist.")
    return parser.parse_args(argv)


def _resolve_date(args: argparse.Namespace) -> date:
    if args.date:
        return date.fromisoformat(args.date)
    if args.yesterday:
        return date.today() - timedelta(days=1)
    return date.today()
