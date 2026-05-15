"""engram rebuild-triggers — re-extract triggers from memory bodies.

Why: the v6 migration dropped v1 triggers (the shape changed, the values
couldn't be mapped cleanly because v1 stored head-prefixes but v2 needs
full token sequences). Memories kept their bodies + kind + scope, but
lost their triggers. This CLI re-runs `formation.extract_candidates`
on each active memory body and re-inserts the derived triggers.

Also useful as a general "reset triggers to what the extractor thinks"
tool: drops all triggers for target memories, re-extracts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import db
from ..formation.candidates import extract_candidates
from ..formation.triggers import insert_candidate_triggers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engram rebuild-triggers")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the DB. Defaults to $ENGRAM_DB or ~/.claude/tool-engrams/db.sqlite.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be extracted; don't modify the DB.",
    )
    parser.add_argument(
        "--only-triggerless",
        action="store_true",
        help=(
            "Only rebuild for memories that currently have zero triggers. "
            "Default: rebuild all active memories (drops existing triggers)."
        ),
    )
    args = parser.parse_args(argv)

    target = Path(args.db).expanduser() if args.db else db.db_path()

    with db.session(target) as conn:
        if args.only_triggerless:
            rows = conn.execute("""
                SELECT m.id, m.name, m.body FROM memories m
                LEFT JOIN triggers t ON t.memory_id = m.id
                WHERE m.archived_ts IS NULL AND t.id IS NULL
                GROUP BY m.id
            """).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, body FROM memories WHERE archived_ts IS NULL"
            ).fetchall()

        summary: dict = {
            "mode": "dry_run" if args.dry_run else "applied",
            "only_triggerless": args.only_triggerless,
            "total_memories_considered": len(rows),
            "rebuilt": 0,
            "no_triggers_extracted": 0,
            "extracted_triggers": [],
        }

        with db.transaction(conn):
            for row in rows:
                mid = row["id"]
                body = row["body"] or ""
                candidates = extract_candidates(body)
                if not candidates:
                    summary["no_triggers_extracted"] += 1
                    continue

                if not args.dry_run:
                    # Wipe existing triggers (even if this memory already has some) —
                    # we're re-deriving from the body as source of truth.
                    conn.execute("DELETE FROM triggers WHERE memory_id = ?", (mid,))
                    insert_candidate_triggers(conn, mid, candidates)

                summary["rebuilt"] += 1
                summary["extracted_triggers"].append({
                    "id": mid,
                    "name": row["name"],
                    "triggers": [
                        {
                            "kind": c.kind,
                            "tokens": list(c.tokens) if c.tokens else None,
                            "path_pattern": c.path_pattern,
                            "source": c.source,
                        }
                        for c in candidates
                    ],
                })

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
