"""Consolidation-recommendation CLI: `engram recommend --list` / `--close` (WS5.2).

The nightly consolidation agent emits durable, cross-run advisories
(`consolidation_recommendations`). This is the maintainer's side of that loop:

- `--list` prints the standing OPEN backlog (one row per title, newest-status,
  critical excluded) across ALL runs — wider than the agent's bounded context
  window, so a human sees every still-outstanding item, including ones that aged
  out of the agent's view.
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
from ..utils import is_consolidation_child


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    with db.session() as conn:
        if args.close is not None:
            # Maintainer-only boundary, enforced in code (not just the prompt):
            # the nightly consolidation agent runs with the full engram verb set,
            # so refuse --close under it. Otherwise the agent could silently close
            # a critical (code-bug/data-safety) rec that only a human can verify.
            if is_consolidation_child():
                print(json.dumps({"action": "forbidden", "reason": "agent_context",
                                  "title": args.close}))
                return 1
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

        # Default (and explicit --list): show the open backlog. Unlike the agent's
        # bounded context view, the maintainer list spans ALL runs so an aged-out
        # still-open rec stays visible (and closable).
        rows = runs.open_recommendations(conn, runs.ALL_RUNS)
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
