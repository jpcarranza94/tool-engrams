"""Insert a handful of example memories so `engram pretool` has something to find."""

from __future__ import annotations

import json
import time

from .. import db, memory_store


SEED_MEMORIES = [
    {
        "name": "psql replica is read-only",
        "description": "Production replica is SELECT-only, no writes allowed.",
        "body": (
            "`psql -h replica.internal` connects to a PostgreSQL production replica. "
            "SELECT-only — the connection cannot mutate state. Never attempt "
            "INSERT/UPDATE/DELETE; they will fail and the replica is not a safe "
            "place to test writes."
        ),
        "kind": "hint",
        "scope": "global",
        "triggers": [
            {"kind": "token_subseq", "tokens": ["psql", "-h"]},
        ],
    },
    {
        "name": "git commit uses HEREDOC for multi-line messages",
        "description": "Commit message format convention.",
        "body": (
            "For commits with multi-line bodies always use the HEREDOC form: "
            "`git commit -m \"$(cat <<'EOF'\\n...\\nEOF\\n)\"` — avoids shell "
            "escaping pitfalls with quotes, backticks, and dollar signs."
        ),
        "kind": "block",
        "scope": "global",
        "triggers": [
            {"kind": "token_subseq", "tokens": ["git", "commit"]},
        ],
    },
    {
        "name": "ssh to production: check VPN first on connection timeout",
        "description": "Recovery hint for ssh to production servers.",
        "body": (
            "If `ssh deploy@production` times out or gives "
            "'Connection refused', the usual cause is the VPN not being "
            "connected. Check VPN state before debugging further."
        ),
        "kind": "hint",
        "scope": "global",
        "triggers": [
            {"kind": "token_subseq", "tokens": ["ssh", "deploy@production"]},
        ],
    },
]


def main() -> int:
    now_ts = int(time.time())
    inserted = 0
    with db.session() as conn, db.transaction(conn):
        for m in SEED_MEMORIES:
            if memory_store.name_exists(conn, m["name"]):
                continue
            mid = memory_store.insert_memory(
                conn,
                name=m["name"],
                description=m["description"],
                body=m["body"],
                kind=m["kind"],
                scope=m["scope"],
                project_slug=None if m["scope"] == "global" else m.get("project_slug"),
                pinned=False,
                created_ts=now_ts,
            )
            _insert_triggers(conn, mid, m["triggers"])
            inserted += 1

    print(json.dumps({"inserted": inserted, "total_seed": len(SEED_MEMORIES)}))
    return 0


def _insert_triggers(conn, memory_id: int, triggers: list[dict]) -> None:
    for t in triggers:
        kind = t["kind"]
        if kind == "token_subseq":
            tokens = list(t["tokens"])
            if not tokens:
                continue
            memory_store.add_token_trigger(conn, memory_id, tokens[0], tokens)
        elif kind == "path_glob":
            memory_store.add_path_trigger(conn, memory_id, t["path_pattern"])
