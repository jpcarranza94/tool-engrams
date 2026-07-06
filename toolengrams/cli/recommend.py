"""Consolidation-recommendation CLI: `engram recommend --list` / `--close` (WS5.2).

The nightly consolidation agent emits durable, cross-run advisories
(`consolidation_recommendations`). This is the maintainer's side of that loop:

- `--list` prints the standing OPEN backlog (one row per title, newest-status,
  critical excluded) — the same view the agent is shown so a human can see what
  is still outstanding.
- `--close "<title>"` marks every stored row with that (casefolded) title `done`.

Why a CLI close at all: `severity='critical'` (code-bug / data-safety) items are
deliberately kept OUT of the agent's injected backlog — the agent can't verify a
maintainer's code fix actually shipped and would re-open them forever. So those
close only here. A manual close is STICKY: `resolve_recommendation` stamps the
rows `done`, which drops the title out of `open_recommendations`, so the nightly
agent is never prompted to re-raise it (a genuine fresh re-detection is still
allowed — that is a real recurrence, not a clobber).
"""

from __future__ import annotations

import argparse
import json
import time

from .. import db
from ..consolidation import runs


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    with db.session() as conn:
        if args.close is not None:
            # Titles are stripped at insert (report_parse.extract_recommendations),
            # so strip the hand-typed arg too or a padded --close never matches.
            title = args.close.strip()
            now_ts = int(time.time())
            with db.transaction(conn):
                updated = runs.resolve_recommendation(conn, title, now_ts=now_ts)
            if updated == 0:
                print(json.dumps({"action": "not_found", "title": title}))
                return 1
            print(json.dumps({
                "action": "closed", "title": title,
                "rows_updated": updated, "resolved_ts": now_ts,
            }))
            return 0

        # Default (and explicit --list): show the open backlog.
        rows = runs.open_recommendations(conn, runs.OPEN_BACKLOG_RUN_WINDOW)
        out = [{"title": r["title"], "severity": r["severity"],
                "detail": r["detail"], "run_date": r["run_date"],
                "issue_url": r["issue_url"]} for r in rows]
        print(json.dumps({"action": "list", "open": out, "count": len(out)}))
        return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="engram recommend",
        description="List or close nightly-consolidation recommendations.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true",
                       help="Show the standing OPEN backlog (default).")
    group.add_argument("--close", metavar="TITLE", default=None,
                       help="Mark every recommendation with this (casefolded) "
                            "title as done — the sticky manual-close path.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
