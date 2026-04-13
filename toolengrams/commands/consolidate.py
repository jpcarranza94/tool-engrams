"""Nightly consolidation: `engram consolidate` — sleep for memories.

Replays the day's sessions, evaluates memory surfacing quality, auto-archives
dead memories, flags stale ones, and cleans up old session_surfaces.

Phase 2 is mechanical (no LLM). Phase 3 will add Haiku-judged evaluation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

from .. import db
from ..consolidation.adjust import run_mechanical_adjustments
from ..consolidation.collect import collect_sessions
from ..consolidation.episodes import (
    extract_correction_episodes,
    extract_surfacing_episodes,
)
from ..consolidation.judge import (
    apply_discoveries,
    apply_verdicts,
    judge_episodes,
)
from ..consolidation.report import format_report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Schedule management (doesn't need DB).
    if args.install_schedule:
        from ..schedule import install_schedule
        path = install_schedule(use_llm=args.llm)
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

        # Phase 1: Collect sessions.
        sessions = collect_sessions(target)

        # Phase 2: Extract episodes.
        start_ts = int(datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        end_ts = start_ts + 86400

        surfacing_episodes = extract_surfacing_episodes(conn, sessions, (start_ts, end_ts))
        correction_episodes = extract_correction_episodes(sessions)

        # Phase 3a: LLM evaluation (optional).
        llm_strengthened = 0
        llm_weakened = 0
        llm_discovered = 0
        llm_error = None
        if args.llm and not args.dry_run:
            existing_names = [
                r["name"] for r in conn.execute(
                    "SELECT name FROM memories WHERE archived_ts IS NULL"
                ).fetchall()
            ]
            judgment = judge_episodes(
                surfacing_episodes, correction_episodes, existing_names,
            )
            if judgment.error:
                llm_error = judgment.error
                print(f"engram consolidate: LLM warning: {judgment.error}", file=sys.stderr)
            else:
                llm_strengthened, llm_weakened = apply_verdicts(conn, judgment.verdicts)
                llm_discovered = apply_discoveries(conn, judgment.discoveries)

        # Phase 3b: Mechanical adjustments (always runs unless dry-run).
        if args.dry_run:
            from ..consolidation.adjust import AdjustmentReport
            adjustment = AdjustmentReport()
        else:
            adjustment = run_mechanical_adjustments(conn)

        # Phase 4: Report.
        report_text = format_report(
            target_date=target.isoformat(),
            sessions=sessions,
            surfacing_episodes=surfacing_episodes,
            correction_episodes=correction_episodes,
            adjustment=adjustment,
            is_dry_run=args.dry_run,
        )
        if args.llm:
            report_text += f"\n\nLLM evaluation (Haiku):"
            report_text += f"\n  Strengthened: {llm_strengthened}"
            report_text += f"\n  Weakened: {llm_weakened}"
            report_text += f"\n  Discovered: {llm_discovered}"
            if llm_error:
                report_text += f"\n  Error: {llm_error}"

        # Log the run (unless dry-run).
        if not args.dry_run:
            now_ts = int(time.time())
            conn.execute(
                "INSERT OR REPLACE INTO consolidation_runs "
                "(run_date, started_ts, completed_ts, sessions_scanned, "
                "episodes_evaluated, memories_strengthened, memories_weakened, "
                "memories_archived, memories_discovered, report) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    target.isoformat(),
                    now_ts,
                    now_ts,
                    len(sessions),
                    len(surfacing_episodes),
                    llm_strengthened,
                    llm_weakened,
                    len(adjustment.archived_ids),
                    llm_discovered,
                    report_text,
                ),
            )

        if args.json:
            print(json.dumps({
                "status": "dry_run" if args.dry_run else "completed",
                "run_date": target.isoformat(),
                "sessions_scanned": len(sessions),
                "surfacing_episodes": len(surfacing_episodes),
                "correction_episodes": len(correction_episodes),
                "archived": len(adjustment.archived_ids),
                "stale": len(adjustment.stale_ids),
                "surfaces_cleaned": adjustment.surfaces_cleaned,
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
                        help="Extract and report only; don't modify DB.")
    parser.add_argument("--force", action="store_true",
                        help="Re-run even if already consolidated for this date.")
    parser.add_argument("--llm", action="store_true",
                        help="Use Haiku to evaluate episodes and discover memories (Phase 3).")
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
