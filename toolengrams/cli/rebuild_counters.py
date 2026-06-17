"""engram rebuild-counters — recompute useful_count/noise_count from surfaces.

Why: the per-memory quality ratio q = (useful+1)/(useful+noise+2) drives the
PreToolUse surfacing gate. Its inputs drifted from reality:

  - the v12 migration zeroed useful_count and introduced noise_count at 0, but
    `session_surfaces` kept every pre-v12 'helpful' verdict — so old proven
    memories read useful_count=0 with many helpful surfaces;
  - `restore` historically zeroed useful_count, discarding earned reputation;
  - the old judge bumped +1 per call regardless of how many surfaces it closed.

`session_surfaces.outcome` is the durable ground truth. This re-derives the
counters from it: useful_count = #helpful surfaces, noise_count = #noise
surfaces. Idempotent — safe to run repeatedly. Pairs with the judge now bumping
by rows-closed, so counters stay consistent going forward. See docs/adr/0013.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import db, memory_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engram rebuild-counters")
    parser.add_argument("--db", default=None,
                        help="Path to the DB. Defaults to $ENGRAM_DB or <engram home>/db.sqlite.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the counter changes; don't write them.")
    args = parser.parse_args(argv)

    target = Path(args.db).expanduser() if args.db else db.db_path()

    with db.session(target) as conn:
        # Memories whose stored counters disagree with the surface ground truth.
        drifted = conn.execute(
            "SELECT m.id, m.name, m.useful_count, m.noise_count, "
            "  COALESCE(SUM(s.outcome = 'helpful'), 0) AS true_useful, "
            "  COALESCE(SUM(s.outcome = 'noise'),   0) AS true_noise "
            "FROM memories m LEFT JOIN session_surfaces s ON s.memory_id = m.id "
            "GROUP BY m.id "
            "HAVING m.useful_count != true_useful OR m.noise_count != true_noise "
            "ORDER BY true_useful DESC"
        ).fetchall()

        changes = [{
            "id": r["id"], "name": r["name"],
            "useful_count": [r["useful_count"], r["true_useful"]],
            "noise_count": [r["noise_count"], r["true_noise"]],
        } for r in drifted]

        summary = {
            "mode": "dry_run" if args.dry_run else "applied",
            "drifted_memories": len(changes),
            "changes": changes,
        }

        if not args.dry_run and changes:
            with db.transaction(conn):
                memory_store.recount_from_surfaces(conn)  # all memories

        print(json.dumps(summary, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
