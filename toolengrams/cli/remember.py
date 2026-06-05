"""Formation CLI: `engram remember` — author a memory with auto-extracted triggers.

Reads body text from a positional arg or stdin, optionally accepts metadata
flags (name, description, type, scope, pinned, extra triggers), deterministically
extracts trigger candidates via `formation.extract_candidates`, consolidates
against existing vocabulary for reporting, inserts the memory + triggers inside
a single transaction, and emits a JSON summary on stdout.

Dry-run mode (`--dry-run`) prints what *would* be inserted without touching
the DB — useful for interactive confirmation flows and for the `/remember`
skill proposal step.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from .. import db, memory_store
from ..formation import (
    FormationCandidate,
    consolidate_vocabulary,
    extract_candidates,
    extras_to_candidates,
    find_overlapping_memory,
    insert_candidate_triggers,
    scan_for_secrets,
    update_existing_memory,
)
from ..utils import slugify_cwd

VALID_KINDS = {"block", "hint"}
VALID_SCOPES = {"global", "project"}
DEFAULT_KIND = "hint"
DEFAULT_SCOPE = "project"

_NAME_MAX = 80


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    rc, body, name, description = _validate_and_parse(args)
    if rc is not None:
        return rc

    extra_triggers = _parse_extra_triggers(args.extra_trigger or [])
    candidates, all_triggers = _resolve_triggers(body, args, extra_triggers)

    if not all_triggers:
        print(json.dumps({
            "error": "no_triggers",
            "message": (
                "No tool-call triggers could be extracted from the body. "
                "Include backticked commands (e.g. `git push`, `docker compose up`) "
                "or file paths so the memory has something to bind to. "
                "A memory without triggers will never surface."
            ),
            "body_preview": body[:200],
        }))
        return 1

    # --- Gate 2: reject memories containing secrets ---
    secret_findings = scan_for_secrets(body)
    if not args.dry_run and secret_findings:
        print(json.dumps({
            "error": "contains_secrets",
            "message": (
                "Memory body appears to contain sensitive data and was rejected. "
                "Never store API keys, passwords, tokens, or connection strings "
                "in memories. Describe the tool pattern without including the "
                "actual credentials."
            ),
            "findings": secret_findings,
            "body_preview": body[:200],
        }))
        return 1

    with db.session() as conn:
        candidates = consolidate_vocabulary(conn, candidates)
        project_slug = _resolve_project_slug(
            args.scope, args.project_slug, args.project_cwd,
        )
        existing = find_overlapping_memory(conn, name, all_triggers, project_slug)

        common = dict(
            name=name, description=description, body=body,
            kind=args.kind, scope=args.scope, project_slug=project_slug,
            pinned=args.pinned, candidates=candidates,
            extra_triggers=extra_triggers,
        )

        if args.dry_run:
            payload = _build_payload(
                memory_id=None, action="dry_run",
                existing_match=existing, **common,
            )
            print(json.dumps(payload, indent=2))
            return 0

        if existing:
            memory_id = update_existing_memory(
                conn=conn, existing_id=existing["id"],
                name=name, description=description, body=body,
                kind=args.kind, pinned=args.pinned,
                candidates=candidates, extra_triggers=extra_triggers,
            )
            action = "updated"
        else:
            memory_id = _insert(conn=conn, **common)
            action = "inserted"
            existing = None

        payload = _build_payload(
            memory_id=memory_id, action=action,
            existing_match=existing, **common,
        )
        print(json.dumps(payload))
        return 0


def _validate_and_parse(
    args: argparse.Namespace,
) -> tuple[int | None, str, str, str]:
    """Validate inputs and extract body/name/description.

    Returns (error_code, body, name, description). error_code is None on success.
    """
    body = _read_body(args)
    if not body:
        print("engram remember: body is empty (pass text as arg, -, or pipe stdin)", file=sys.stderr)
        return 2, "", "", ""

    if args.kind not in VALID_KINDS:
        print(f"engram remember: --kind must be one of {sorted(VALID_KINDS)}", file=sys.stderr)
        return 2, "", "", ""
    if args.scope not in VALID_SCOPES:
        print(f"engram remember: --scope must be one of {sorted(VALID_SCOPES)}", file=sys.stderr)
        return 2, "", "", ""

    name = args.name or _synthesize_name(body)
    description = args.description or ""
    return None, body, name, description


def _resolve_triggers(
    body: str,
    args: argparse.Namespace,
    extra_triggers: list[dict[str, Any]],
) -> tuple[list[FormationCandidate], list[FormationCandidate]]:
    """Build candidates from explicit flags or body extraction.

    Returns (candidates, all_triggers) where all_triggers includes extras.
    """
    explicit = _parse_explicit_triggers(args.trigger or [], args.path or [])
    candidates = explicit if explicit else extract_candidates(body)

    all_triggers = candidates + [
        FormationCandidate(
            kind=t["kind"],
            tokens=tuple(t.get("tokens") or ()),
            path_pattern=t.get("path_pattern"),
            source="extra",
        )
        for t in extra_triggers
    ]
    return candidates, all_triggers


# ---------- argparse ----------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram remember")
    parser.add_argument("text", nargs="?", default=None,
                        help="Memory body. Use '-' or omit to read from stdin.")
    parser.add_argument("--name", default=None, help="Short human-readable name.")
    parser.add_argument("--description", default=None, help="One-line description.")
    parser.add_argument("--kind", default=DEFAULT_KIND,
                        help=(f"block|hint (default {DEFAULT_KIND}). "
                              "block: PreToolUse denies + injects context. "
                              "hint: PostToolUseFailure injects context on error."))
    parser.add_argument("--scope", default=DEFAULT_SCOPE,
                        help=f"global|project (default {DEFAULT_SCOPE})")
    parser.add_argument("--project-slug", default=None,
                        help="Override the project slug (defaults to slugified cwd for scope=project).")
    parser.add_argument("--project-cwd", default=None,
                        help=("Explicit working directory used to compute the project slug "
                              "for scope=project. Useful when calling from a subprocess "
                              "(e.g. the watcher) whose own cwd differs from the user's. "
                              "Falls back to os.getcwd()."))
    parser.add_argument("--pinned", action="store_true",
                        help="Pin this memory so reinforcement doesn't gate it.")
    parser.add_argument("--trigger", action="append", default=None,
                        metavar="CMD_PREFIX",
                        help=("Command prefix to bind to (repeatable). "
                              "e.g. --trigger 'git push --force' "
                              "--trigger 'git push -f'"))
    parser.add_argument("--path", action="append", default=None,
                        metavar="GLOB",
                        help="Path glob to bind to (repeatable). e.g. --path '**/*.py'")
    parser.add_argument("--extra-trigger", action="append", default=None,
                        metavar="SPEC",
                        help="token_subseq:git,push | path_glob:**/*.py")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and report candidates; do not insert.")
    return parser.parse_args(argv)


# ---------- body / name ----------


def _read_body(args: argparse.Namespace) -> str:
    if args.text and args.text != "-":
        return args.text.strip()
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read().strip()


def _synthesize_name(body: str) -> str:
    """First non-empty line, trimmed to _NAME_MAX chars."""
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:_NAME_MAX]
    return body[:_NAME_MAX].strip() or "unnamed memory"


def _resolve_project_slug(
    scope: str,
    slug_override: str | None,
    cwd_override: str | None,
) -> str | None:
    if scope != "project":
        return None
    if slug_override:
        return slug_override
    cwd = cwd_override or os.getcwd()
    return slugify_cwd(cwd)


# ---------- triggers ----------


def _parse_explicit_triggers(
    cmd_prefixes: list[str],
    path_globs: list[str],
) -> list[FormationCandidate]:
    """Parse --trigger and --path flags into FormationCandidates.

    --trigger takes a space-separated list of required tokens, e.g.
    'git push --force'. Stored as a token_subseq trigger matched in order.
    """
    out: list[FormationCandidate] = []
    for prefix in cmd_prefixes:
        prefix = prefix.strip()
        if not prefix:
            continue
        tokens = tuple(prefix.split())
        out.append(FormationCandidate(
            kind="token_subseq",
            tokens=tokens,
            source="explicit",
        ))
    for glob in path_globs:
        glob = glob.strip()
        if not glob:
            continue
        out.append(FormationCandidate(
            kind="path_glob",
            path_pattern=glob,
            source="explicit",
        ))
    return out


def _parse_extra_triggers(specs: list[str]) -> list[dict[str, Any]]:
    """Parse --extra-trigger SPEC strings into dict rows.

    Formats:
      path_glob:**/*.py
      token_subseq:git,push
    """
    out: list[dict[str, Any]] = []
    for spec in specs:
        parts = spec.split(":")
        kind = parts[0]
        if kind == "path_glob" and len(parts) == 2:
            out.append({"kind": "path_glob", "path_pattern": parts[1]})
        elif kind == "token_subseq" and len(parts) == 2:
            tokens = tuple(t for t in parts[1].split(",") if t)
            if tokens:
                out.append({"kind": "token_subseq", "tokens": tokens})
        else:
            raise SystemExit(f"engram remember: malformed --extra-trigger {spec!r}")
    return out


def _insert(
    *,
    conn,
    name: str,
    description: str,
    body: str,
    kind: str,
    scope: str,
    project_slug: str | None,
    pinned: bool,
    candidates: list[FormationCandidate],
    extra_triggers: list[dict[str, Any]],
) -> int:
    now_ts = int(time.time())
    with db.transaction(conn):
        memory_id = memory_store.insert_memory(
            conn, name=name, description=description, body=body, kind=kind,
            scope=scope, project_slug=project_slug, pinned=pinned, created_ts=now_ts,
        )
        insert_candidate_triggers(conn, memory_id, candidates)
        insert_candidate_triggers(conn, memory_id, extras_to_candidates(extra_triggers))
    return memory_id



# ---------- output payload ----------


def _build_payload(
    *,
    memory_id: int | None,
    name: str,
    description: str,
    body: str,
    kind: str,
    scope: str,
    project_slug: str | None,
    pinned: bool,
    candidates: list[FormationCandidate],
    extra_triggers: list[dict[str, Any]],
    action: str = "inserted",
    existing_match: dict | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "action": action,
        "memory": {
            "id": memory_id,
            "name": name,
            "description": description,
            "kind": kind,
            "scope": scope,
            "project_slug": project_slug,
            "pinned": pinned,
            "body_chars": len(body),
        },
        "extracted_triggers": [_candidate_to_dict(c) for c in candidates],
        "extra_triggers": extra_triggers,
        "counts": {
            "extracted": len(candidates),
            "extra": len(extra_triggers),
            "total": len(candidates) + len(extra_triggers),
        },
    }
    if existing_match:
        result["existing_match"] = existing_match
    return result


def _candidate_to_dict(c: FormationCandidate) -> dict[str, Any]:
    return {
        "kind": c.kind,
        "tokens": list(c.tokens) if c.tokens else None,
        "path_pattern": c.path_pattern,
        "source": c.source,
        "existing_memories": c.existing_memories,
    }


if __name__ == "__main__":
    raise SystemExit(main())
