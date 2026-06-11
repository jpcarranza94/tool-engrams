"""Lifecycle CLI: `engram edit` — in-place content correction (ADR-0007).

The counter-preserving analogue of `engram trigger` narrowing, for bodies:
updates body (and optionally name/description) while PRESERVING the memory's
id, reinforcement counters, surfaces, and triggers — ending the
forget-and-re-remember dance that destroyed history and could be interrupted
halfway. Stamps `last_verified_ts`: a deliberate correction is the strongest
freshness signal the staleness audit gets.

Interactive sessions and the consolidation agent only — deliberately NOT in
either watcher allowlist (autonomous rewriting of trusted bodies is tampering
surface; see ADR-0007).
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import db, memory_store
from ..formation import extract_candidates, insert_candidate_triggers, scan_for_secrets


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not (args.body or args.name or args.description):
        print("engram edit: nothing to change — pass --body, --name, and/or "
              "--description", file=sys.stderr)
        return 2
    if args.re_extract_triggers and not args.body:
        print("engram edit: --re-extract-triggers needs --body", file=sys.stderr)
        return 2

    # Same gate as `remember`: a correction must not smuggle credentials in.
    if args.body:
        findings = scan_for_secrets(args.body)
        if findings:
            print(json.dumps({
                "error": "contains_secrets",
                "message": "New body appears to contain sensitive data; rejected.",
                "findings": findings,
            }))
            return 1

    with db.session() as conn:
        mem = _resolve(conn, args.target)
        if mem is None:
            print(json.dumps({"error": "not_found", "query": args.target}))
            return 1

        new_body = args.body if args.body is not None else mem.body
        with db.transaction(conn):
            memory_store.set_content(
                conn, mem.id, body=new_body,
                name=args.name, description=args.description,
            )
            retriggered = 0
            if args.re_extract_triggers:
                memory_store.delete_triggers_for(conn, mem.id)
                candidates = extract_candidates(new_body)
                insert_candidate_triggers(conn, mem.id, candidates)
                retriggered = memory_store.count_triggers_for(conn, mem.id)

        updated = memory_store.get(conn, mem.id)
        print(json.dumps({
            "action": "edited",
            "memory_id": mem.id,
            "name": updated.name,
            "preserved": {
                "surface_count": updated.surface_count,
                "useful_count": updated.useful_count,
                "noise_count": updated.noise_count,
            },
            "body_chars": len(updated.body),
            "triggers_re_extracted": retriggered if args.re_extract_triggers else None,
        }))
        return 0


def _resolve(conn, target: str):
    """Numeric target → exact id; otherwise name lookup (exact → fuzzy,
    mirroring `engram forget`)."""
    if target.isdigit():
        return memory_store.get(conn, int(target))
    return memory_store.find_by_name(conn, target)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engram edit")
    parser.add_argument("target", help="Memory id or name (exact or fuzzy).")
    parser.add_argument("--body", default=None, help="Replacement body text.")
    parser.add_argument("--name", default=None, help="Replacement name.")
    parser.add_argument("--description", default=None, help="Replacement description.")
    parser.add_argument("--re-extract-triggers", action="store_true",
                        help="Drop existing triggers and re-extract from the new "
                             "body (default: triggers untouched).")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
