"""Eval CLI: `engram quarantine <id> --reason` — pull a harmful memory out of
circulation, reversibly (ADR-0007).

The evaluation watcher's emergency brake. By construction it can only do three
reversible, audited things:

  1. archive the memory — actually OUT of retrieval immediately (the match
     queries exclude archived rows); reversible via `engram forget --restore`
  2. record a `quarantined` event with the reason (the audit trail
     consolidation and `engram monitor` read)
  3. mark the memory's unjudged surfaces `noise` (scoped to --session-id when
     given) so the reinforcement signal reflects the incident

Id-only (no fuzzy names), one memory at a time, no hard delete — the verbs
that destroy stay human/consolidation-tier.
"""

from __future__ import annotations

import argparse
import json

from .. import db, memory_store
from ..retrieval import session_state
from ..watcher import runs_store
from ..watcher.log import _log


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    with db.session() as conn:
        mem = memory_store.get(conn, args.memory_id)
        if mem is None:
            print(json.dumps({"error": "not_found", "memory_id": args.memory_id}))
            return 1

        with db.transaction(conn):
            # Archive (NOT soft-demote): a soft-demoted memory still matches and
            # surfaces — with zero judgments the gate can't suppress it. Archive
            # is the only state retrieval excludes; restore reverses it.
            memory_store.archive(conn, mem.id)
            surfaces_marked = session_state.mark_unmarked_noise(
                conn, mem.id, args.session_id,
            )
            runs_store.record_cli_event(
                conn, kind="quarantined", memory_id=mem.id,
                memory_name=mem.name, detail=args.reason,
            )

        _log(f"QUARANTINE memory={mem.id} name={mem.name!r} "
             f"surfaces_marked={surfaces_marked} reason={args.reason[:120]!r}")
        print(json.dumps({
            "action": "quarantined",
            "memory_id": mem.id,
            "name": mem.name,
            "reason": args.reason,
            "surfaces_marked_noise": surfaces_marked,
            "out_of_circulation": True,
            "reversible": "engram forget --restore '<name>' restores it",
        }))
        return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engram quarantine")
    parser.add_argument("memory_id", type=int,
                        help="Memory id (ids only — no fuzzy name matching).")
    parser.add_argument("--reason", required=True,
                        help="Why this memory is harmful (goes to the audit trail).")
    parser.add_argument("--session-id", default=None,
                        help="Scope the noise-marking to one work session "
                             "(the eval watcher passes its session).")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
