"""Trigger surgery CLI: `engram trigger <memory_id> ...` — add / remove / list
triggers on an existing memory WITHOUT recreating it (counters and history are
preserved).

This is consolidation's lever for the fact that a `noise` verdict means the
TRIGGER over-matched, not that the content is bad: narrow a broad glob, drop
a redundant path on a hybrid that already has a command trigger, or rebind to the
real command moment — instead of archiving a useful memory. A `forget` + new
`remember` would reset useful_count / noise_count; this keeps them.

  engram trigger 42 --add-trigger "git push --force"   # repeatable
  engram trigger 42 --add-path "**/migrations/*.py"     # repeatable
  engram trigger 42 --remove 17 --remove 18             # trigger ids, repeatable
  engram trigger 42 --list                              # show current triggers

A remove + add in one call is a "replace" (narrow a trigger). Adds are validated
through the same chokepoint as `engram remember` (a malformed first token is
rejected). The command refuses to leave a memory with zero triggers — a
triggerless memory can never surface.
"""

from __future__ import annotations

import argparse
import json

from .. import db, memory_store
from ..formation import FormationCandidate, insert_candidate_triggers


class _WouldOrphan(Exception):
    """Raised to roll back a mutation that would leave the memory triggerless."""


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    add_candidates: list[FormationCandidate] = []
    for phrase in args.add_trigger or []:
        toks = tuple(phrase.split())
        if toks:
            add_candidates.append(FormationCandidate(
                kind="token_subseq", tokens=toks, source="trigger-cli"))
    for glob in args.add_path or []:
        if glob.strip():
            add_candidates.append(FormationCandidate(
                kind="path_glob", path_pattern=glob.strip(),
                access_mode=args.access_mode, source="trigger-cli"))
    removes = list(args.remove or [])

    with db.session() as conn:
        mem = memory_store.get(conn, args.memory_id)
        if mem is None:
            print(json.dumps({"error": "not_found", "memory_id": args.memory_id}))
            return 1

        existing = {t.id: t for t in memory_store.triggers_for(conn, args.memory_id)}

        if args.list or (not add_candidates and not removes):
            print(json.dumps({
                "action": "list",
                "memory_id": args.memory_id,
                "name": mem.name,
                "triggers": [_trigger_dict(t) for t in existing.values()],
            }))
            return 0

        bad = [r for r in removes if r not in existing]
        if bad:
            print(json.dumps({
                "error": "not_a_trigger_of_memory",
                "memory_id": args.memory_id, "trigger_ids": bad,
            }))
            return 1

        try:
            with db.transaction(conn):
                for tid in removes:
                    memory_store.delete_trigger(conn, tid)
                added = insert_candidate_triggers(conn, args.memory_id, add_candidates)
                remaining = memory_store.count_triggers_for(conn, args.memory_id)
                if remaining == 0:
                    raise _WouldOrphan()
        except _WouldOrphan:
            print(json.dumps({
                "error": "would_orphan",
                "message": ("This change would leave the memory with no triggers, so "
                            "it could never surface. Add a replacement trigger in the "
                            "same call, or `engram forget` it instead."),
                "memory_id": args.memory_id,
            }))
            return 1

        current = memory_store.triggers_for(conn, args.memory_id)
        print(json.dumps({
            "action": "updated",
            "memory_id": args.memory_id,
            "name": mem.name,
            "added": added,
            "add_requested": len(add_candidates),
            "removed": len(removes),
            "triggers": [_trigger_dict(t) for t in current],
        }))
        return 0


def _trigger_dict(t) -> dict:
    return {
        "id": t.id,
        "kind": t.kind,
        "tokens": t.tokens if t.kind == "token_subseq" else None,
        "path_pattern": t.path_pattern,
        "access_mode": t.access_mode if t.kind == "path_glob" else None,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram trigger")
    parser.add_argument("memory_id", type=int, help="The memory's integer id.")
    parser.add_argument("--add-trigger", action="append", metavar="PHRASE",
                        help="Token trigger phrase to add (repeatable), e.g. 'git push --force'.")
    parser.add_argument("--add-path", action="append", metavar="GLOB",
                        help="Path glob to add (repeatable), e.g. '**/migrations/*.py'.")
    parser.add_argument("--access-mode", choices=("write", "read", "any"), default="write",
                        help=("Access intent for --add-path globs (default write): "
                              "'write' fires only on Edit/Write/MultiEdit/NotebookEdit, "
                              "'read' only on Read/Grep/Glob, 'any' on either. "
                              "Use 'any' to restore the pre-#63 fire-on-read behavior."))
    parser.add_argument("--remove", action="append", type=int, metavar="TRIGGER_ID",
                        help="Trigger id to remove (repeatable; see --list for ids).")
    parser.add_argument("--list", action="store_true",
                        help="List the memory's current triggers and exit.")
    return parser.parse_args(argv)
