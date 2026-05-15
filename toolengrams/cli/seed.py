"""Insert a handful of example memories so `engram pretool` has something to find."""

from __future__ import annotations

import json
import time

from .. import db


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
            existing = conn.execute(
                "SELECT id FROM memories WHERE name = ?", (m["name"],)
            ).fetchone()
            if existing:
                continue
            cur = conn.execute(
                "INSERT INTO memories "
                "(name, description, body, kind, scope, project_slug, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    m["name"],
                    m["description"],
                    m["body"],
                    m["kind"],
                    m["scope"],
                    None if m["scope"] == "global" else m.get("project_slug"),
                    now_ts,
                ),
            )
            mid = cur.lastrowid
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
            conn.execute(
                "INSERT INTO triggers "
                "(memory_id, kind, first_token, tokens_json) "
                "VALUES (?, 'token_subseq', ?, ?)",
                (memory_id, tokens[0], json.dumps(tokens)),
            )
        elif kind == "path_glob":
            conn.execute(
                "INSERT INTO triggers (memory_id, kind, path_pattern) "
                "VALUES (?, 'path_glob', ?)",
                (memory_id, t["path_pattern"]),
            )
