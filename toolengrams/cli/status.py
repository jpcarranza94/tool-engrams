"""engram status — memory health dashboard.

Human-readable summary on a tty; JSON when piped or with --json, so
scripts and hooks that parse the output keep working unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .. import db, memory_store, pause
from ..consolidation import runs
from ..consolidation.schedule import is_installed as schedule_is_installed


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    with db.session() as conn:
        health = memory_store.health_stats(conn)
        last_run = runs.last_run(conn)

        result = {
            "kill_switch": {
                "disabled": pause.is_disabled(),
                "pause_flag": pause.flag_path().exists(),
                "env_override": os.environ.get("ENGRAM_DISABLED") or None,
            },
            "memories": {
                "active": health["active"],
                "archived": health["archived"],
                "total_surfaces": health["total_surfaces"],
                "total_useful": health["total_useful"],
            },
            "triggers": health["triggers_by_kind"],
            "last_consolidation": dict(last_run) if last_run else None,
            "schedule_installed": schedule_is_installed(),
        }

    if args.json or not sys.stdout.isatty():
        print(json.dumps(result, indent=2))
    else:
        print(_format_human(result))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engram status")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output (also the default when piped).")
    return parser


def _format_human(result: dict) -> str:
    lines = ["ToolEngrams status"]

    if result["kill_switch"]["disabled"]:
        env = result["kill_switch"]["env_override"]
        source = f"ENGRAM_DISABLED={env}" if env else "engram pause"
        lines.append(f"  system      PAUSED via {source} — 'engram resume' to re-enable")
    else:
        lines.append("  system      active")

    mem = result["memories"]
    lines.append(f"  memories    {mem['active']} active, {mem['archived']} archived")
    lines.append(f"  surfaces    {mem['total_surfaces']} total, "
                 f"{mem['total_useful']} judged useful")

    trig = result["triggers"]
    lines.append(f"  triggers    {trig.get('token_subseq', 0)} token, "
                 f"{trig.get('path_glob', 0)} path")

    last = result["last_consolidation"]
    schedule = "scheduled nightly" if result["schedule_installed"] else "no schedule"
    if last:
        lines.append(f"  nightly     last run {last.get('run_date', '?')}: "
                     f"{last.get('sessions_scanned', 0)} sessions scanned, "
                     f"{last.get('memories_archived', 0)} archived, "
                     f"{last.get('memories_discovered', 0)} discovered ({schedule})")
    else:
        lines.append(f"  nightly     never run ({schedule})")

    lines.append("\n  (engram doctor for wiring checks; --json for machine output)")
    return "\n".join(lines)
