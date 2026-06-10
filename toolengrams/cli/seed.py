"""engram seed — example memories for the post-install smoke test.

The default set is hint-only: a seeded `block` would deny real tool calls
(the old git-commit block denied every commit once per session), which is
a hostile first-run surprise. `--with-block` opts into one block demo on
`git push --force` — a call you won't trip by accident. `--remove` deletes
exactly the seed memories (by exact name) once the smoke test is done.
"""

from __future__ import annotations

import argparse
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
        "kind": "hint",
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

# Opt-in via --with-block: demonstrates the deny path on a call nobody runs
# by accident, instead of booby-trapping everyday commands.
BLOCK_SEED_MEMORIES = [
    {
        "name": "git push --force overwrites co-workers' commits",
        "description": "Block demo: deny --force, suggest --force-with-lease.",
        "body": (
            "Use `git push --force-with-lease` instead — `--force` overwrites "
            "commits co-workers pushed since your last fetch. (This is a "
            "ToolEngrams seed memory demonstrating the block kind: the call "
            "was denied and this text injected. Remove with 'engram seed "
            "--remove'.)"
        ),
        "kind": "block",
        "scope": "global",
        "triggers": [
            {"kind": "token_subseq", "tokens": ["git", "push", "--force"]},
            {"kind": "token_subseq", "tokens": ["git", "push", "-f"]},
        ],
    },
]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.remove:
        return _remove()
    return _insert(with_block=args.with_block)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engram seed")
    parser.add_argument("--with-block", action="store_true",
                        help="Also seed one block-kind demo (denies "
                             "'git push --force' and suggests --force-with-lease).")
    parser.add_argument("--remove", action="store_true",
                        help="Delete the seed memories (exact names only — "
                             "never touches your own memories).")
    return parser


def _insert(with_block: bool) -> int:
    now_ts = int(time.time())
    to_seed = SEED_MEMORIES + (BLOCK_SEED_MEMORIES if with_block else [])
    inserted = []
    skipped = []
    with db.session() as conn, db.transaction(conn):
        for m in to_seed:
            if memory_store.name_exists(conn, m["name"]):
                skipped.append(m)
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
            inserted.append(m)

    for m in inserted:
        print(f"  seeded  [{m['kind']}] {m['name']}  (trigger: {_trigger_label(m)})")
    for m in skipped:
        print(f"  exists  [{m['kind']}] {m['name']}")
    print(f"\n{len(inserted)} seeded, {len(skipped)} already present.")
    print("Smoke test: in a NEW Claude Code session, ask Claude to run "
          "`ssh deploy@production` — the VPN hint should arrive with the call.")
    print("Clean up afterwards with: engram seed --remove")
    return 0


def _remove() -> int:
    names = [m["name"] for m in SEED_MEMORIES + BLOCK_SEED_MEMORIES]
    removed = 0
    with db.session() as conn, db.transaction(conn):
        for name in names:
            mem = memory_store.find_by_name(conn, name, include_archived=True)
            # find_by_name falls back to fuzzy matching; only an exact name
            # hit may be deleted, or a near-miss would nuke a user memory.
            if mem is None or mem.name != name:
                continue
            memory_store.delete_memory(conn, mem.id)
            removed += 1
            print(f"  removed  {name}")
    noun = "memory" if removed == 1 else "memories"
    print(f"\n{removed} seed {noun} removed.")
    return 0


def _trigger_label(m: dict) -> str:
    first = m["triggers"][0]
    if first["kind"] == "token_subseq":
        return " ".join(first["tokens"])
    return first.get("path_pattern", "?")


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
