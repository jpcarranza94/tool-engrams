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
from ..formation.triggers import (
    first_token_looks_like_cli,
    insert_candidate_triggers,
)


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
    parser.add_argument(
        "--drop-malformed",
        action="store_true",
        help=(
            "One-shot cleanup: drop only token_subseq trigger rows whose "
            "first_token can't be a real shell command head (see "
            "formation.triggers.first_token_looks_like_cli). Preserves "
            "valid triggers including user-explicit ones and path_glob "
            "triggers. If a memory ends up with zero triggers after the "
            "drop and its body produces new extracted triggers, re-derive "
            "from body. Operates on ACTIVE memories only — archived "
            "memories with malformed triggers are left as-is since they "
            "don't surface anyway."
        ),
    )
    args = parser.parse_args(argv)

    target = Path(args.db).expanduser() if args.db else db.db_path()

    with db.session(target) as conn:
        if args.drop_malformed:
            return _drop_malformed(conn, args.dry_run)

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


def _drop_malformed(conn, dry_run: bool) -> int:
    """Drop trigger rows whose first_token can't be a real shell command head.

    Only touches token_subseq triggers (path_glob has no first_token) on
    *active* memories. For memories left with zero remaining triggers after
    the drop, attempts to re-derive from the body. A memory that had a
    malformed token_subseq AND a healthy path_glob is NOT counted as
    triggerless — the path_glob still works. The `memories_still_triggerless`
    list flags memories the user may want to fix manually.
    """
    rows = conn.execute(
        "SELECT t.id, t.memory_id, t.first_token, t.tokens_json, m.name "
        "FROM triggers t JOIN memories m ON m.id = t.memory_id "
        "WHERE t.kind = 'token_subseq' AND m.archived_ts IS NULL"
    ).fetchall()

    bad = [r for r in rows if not first_token_looks_like_cli(r["first_token"])]

    summary: dict = {
        "mode": "dry_run" if dry_run else "applied",
        "total_token_subseq_triggers": len(rows),
        "malformed_triggers_found": len(bad),
        "trigger_rows_dropped": 0,
        "memories_left_triggerless": 0,
        "memories_rebuilt_from_body": 0,
        "memories_still_triggerless": [],
        "dropped": [],
    }

    if not bad:
        print(json.dumps(summary, indent=2))
        return 0

    affected_memory_ids = sorted({r["memory_id"] for r in bad})

    if not dry_run:
        with db.transaction(conn):
            for r in bad:
                conn.execute("DELETE FROM triggers WHERE id = ?", (r["id"],))
                # Mirror the stderr trail emitted by insert_candidate_triggers
                # during normal formation so audit grep'ing works uniformly.
                print(
                    f"engram: dropped malformed trigger for memory {r['memory_id']} — "
                    f"first_token {r['first_token']!r} (tokens_json={r['tokens_json']})",
                    file=sys.stderr,
                )
            summary["trigger_rows_dropped"] = len(bad)

            # Re-derive for memories left with no triggers at all.
            for mid in affected_memory_ids:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM triggers WHERE memory_id = ?", (mid,)
                ).fetchone()[0]
                if remaining > 0:
                    continue
                summary["memories_left_triggerless"] += 1
                body_row = conn.execute(
                    "SELECT name, body FROM memories WHERE id = ?", (mid,)
                ).fetchone()
                candidates = extract_candidates(body_row["body"])
                if candidates:
                    inserted = insert_candidate_triggers(conn, mid, candidates)
                    if inserted:
                        summary["memories_rebuilt_from_body"] += 1
                        continue
                summary["memories_still_triggerless"].append(
                    {"id": mid, "name": body_row["name"]}
                )
    else:
        summary["trigger_rows_dropped"] = len(bad)

    summary["dropped"] = [
        {
            "memory_id": r["memory_id"],
            "memory_name": r["name"],
            "first_token": r["first_token"],
            "tokens_json": r["tokens_json"],
        }
        for r in bad
    ]

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
