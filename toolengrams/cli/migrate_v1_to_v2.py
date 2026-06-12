"""engram migrate-v1-to-v2 — best-effort one-shot migration from a v1-era DB to v2.

Why standalone:
  - v1 used `memories.type` (feedback|reference) and `triggers.tool_head` with
    `tool_name` + `head_joined` + `head_length`.
  - v2 uses `memories.kind` (block|hint) and `triggers.token_subseq` with
    `first_token` + `tokens_json`.
  - The normal `_migrate` path in db.py (via v6.sql + v7.sql) handles the
    schema reshape, but this CLI wraps that with an explicit "I ran the
    migration" marker and prints a summary of what was converted for auditing.

Alpha-stage caveat: we assume the source DB is actually at v5 (last v1 state).
If the user's DB is already at v6+, we no-op. If at <v5, we refuse — that's
an older version we never shipped in a public form.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .. import db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engram migrate-v1-to-v2")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the v1 DB. Defaults to $ENGRAM_DB or <engram home>/db.sqlite.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what the migration would do, don't actually run it.",
    )
    args = parser.parse_args(argv)

    target = Path(args.db).expanduser() if args.db else db.db_path()
    if not target.is_file():
        print(json.dumps({
            "error": "db_not_found",
            "path": str(target),
            "message": "No DB found. Nothing to migrate.",
        }))
        return 1

    pre_state = _inspect(target)
    if pre_state["version"] >= 7:
        print(json.dumps({
            "action": "noop",
            "reason": "already_v2",
            "current_version": pre_state["version"],
            "db_path": str(target),
        }))
        return 0
    if pre_state["version"] < 5:
        print(json.dumps({
            "action": "refused",
            "reason": "unsupported_version",
            "current_version": pre_state["version"],
            "message": (
                "DB is older than the last v1 release (v5). We don't support "
                "migrating pre-release schemas. Nuke the DB and restart."
            ),
        }))
        return 1

    if args.dry_run:
        plan = _plan_migration(pre_state)
        print(json.dumps({"action": "dry_run", "plan": plan, "db_path": str(target)}, indent=2))
        return 0

    # The actual migration is the v6.sql + v7.sql chain applied by db.connect().
    # We just open the DB to trigger it, then re-inspect for the summary.
    with db.session(target) as conn:
        post_state = _inspect_open(conn)
        summary = {
            "action": "migrated",
            "db_path": str(target),
            "from_version": pre_state["version"],
            "to_version": post_state["version"],
            "memories_before": pre_state["memory_count"],
            "memories_after": post_state["memory_count"],
            "triggers_before": pre_state["trigger_count"],
            "triggers_after": post_state["trigger_count"],
            "kind_distribution_after": post_state["kind_distribution"],
            "trigger_kind_distribution_after": post_state["trigger_kind_distribution"],
        }
        print(json.dumps(summary, indent=2))
        return 0


def _inspect(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        mem_count = _scalar(conn, "SELECT COUNT(*) FROM memories")
        trig_count = _scalar(conn, "SELECT COUNT(*) FROM triggers")
        # v1 shape has memories.type; v2 has memories.kind. Grab whichever exists.
        mem_cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
        if "type" in mem_cols:
            type_dist = {
                r["type"]: r["count"]
                for r in conn.execute(
                    "SELECT type, COUNT(*) AS count FROM memories "
                    "WHERE archived_ts IS NULL GROUP BY type"
                ).fetchall()
            }
        else:
            type_dist = {}
        return {
            "version": version,
            "memory_count": mem_count,
            "trigger_count": trig_count,
            "type_distribution": type_dist,
        }
    finally:
        conn.close()


def _inspect_open(conn: sqlite3.Connection) -> dict:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    mem_count = _scalar(conn, "SELECT COUNT(*) FROM memories")
    trig_count = _scalar(conn, "SELECT COUNT(*) FROM triggers")
    kind_dist = {
        r["kind"]: r["count"]
        for r in conn.execute(
            "SELECT kind, COUNT(*) AS count FROM memories "
            "WHERE archived_ts IS NULL GROUP BY kind"
        ).fetchall()
    }
    trig_kind_dist = {
        r["kind"]: r["count"]
        for r in conn.execute(
            "SELECT kind, COUNT(*) AS count FROM triggers GROUP BY kind"
        ).fetchall()
    }
    return {
        "version": version,
        "memory_count": mem_count,
        "trigger_count": trig_count,
        "kind_distribution": kind_dist,
        "trigger_kind_distribution": trig_kind_dist,
    }


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    try:
        return conn.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _plan_migration(pre_state: dict) -> dict:
    """Describe what the v6+v7 chain (plus any later migrations) will do."""
    type_dist = pre_state.get("type_distribution") or {}
    return {
        "from_version": pre_state["version"],
        "to_version": db.SCHEMA_VERSION,
        "steps": [
            "v6: drop memory_associations (Hebbian), rebuild triggers with "
            "(first_token, tokens_json) replacing (tool_name, head_joined, head_length)",
            "v7: rebuild memories with kind IN ('block','hint') replacing "
            "type IN ('feedback','reference'). feedback→block, reference→hint.",
        ],
        "value_map": {
            "memories.type=feedback → memories.kind=block": type_dist.get("feedback", 0),
            "memories.type=reference → memories.kind=hint": type_dist.get("reference", 0),
        },
        "caveats": [
            "Existing triggers are dropped and NOT re-generated from memory bodies. "
            "Run `engram recall` after migration to verify; re-seed via `engram remember` "
            "if coverage drops.",
            "Hebbian associations are gone permanently — not a surface track in v2.",
        ],
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
