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

from .. import db
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

VALID_TYPES = {"feedback", "reference"}
VALID_SCOPES = {"global", "project"}
DEFAULT_TYPE = "reference"
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

    conn = db.connect()
    try:
        candidates = consolidate_vocabulary(conn, candidates)
        project_slug = _resolve_project_slug(args.scope, args.project_slug)
        existing = find_overlapping_memory(conn, name, all_triggers, project_slug)

        common = dict(
            name=name, description=description, body=body,
            type_=args.type, scope=args.scope, project_slug=project_slug,
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
                type_=args.type, pinned=args.pinned,
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
    finally:
        conn.close()


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

    if args.type not in VALID_TYPES:
        print(f"engram remember: --type must be one of {sorted(VALID_TYPES)}", file=sys.stderr)
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
            tool_name=t.get("tool_name"),
            head=t.get("head", ()),
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
    parser.add_argument("--type", default=DEFAULT_TYPE,
                        help=f"user|feedback|project|reference (default {DEFAULT_TYPE})")
    parser.add_argument("--scope", default=DEFAULT_SCOPE,
                        help=f"global|project (default {DEFAULT_SCOPE})")
    parser.add_argument("--project-slug", default=None,
                        help="Override the project slug (defaults to slugified cwd for scope=project).")
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
                        help="(Legacy) tool_head:Bash:git,push | path_glob:**/*.py")
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


def _resolve_project_slug(scope: str, override: str | None) -> str | None:
    if scope != "project":
        return None
    if override:
        return override
    cwd = os.environ.get("ENGRAM_PROJECT_CWD") or os.getcwd()
    return slugify_cwd(cwd)


# ---------- triggers ----------


def _parse_explicit_triggers(
    cmd_prefixes: list[str],
    path_globs: list[str],
) -> list[FormationCandidate]:
    """Parse --trigger and --path flags into FormationCandidates.

    --trigger takes a command prefix string like 'git push --force'.
    The full string is stored as head_joined for prefix matching.
    """
    out: list[FormationCandidate] = []
    for prefix in cmd_prefixes:
        prefix = prefix.strip()
        if not prefix:
            continue
        tokens = tuple(prefix.split())
        out.append(FormationCandidate(
            kind="tool_head",
            tool_name="Bash",
            head=tokens,
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
    """Parse --extra-trigger SPEC strings into dict rows."""
    out: list[dict[str, Any]] = []
    for spec in specs:
        parts = spec.split(":")
        kind = parts[0]
        if kind == "path_glob" and len(parts) == 2:
            out.append({"kind": "path_glob", "path_pattern": parts[1]})
        elif kind == "tool_head" and len(parts) == 3:
            tool_name = parts[1]
            head = tuple(t for t in parts[2].split(",") if t)
            out.append({"kind": "tool_head", "tool_name": tool_name, "head": head})
        else:
            raise SystemExit(f"engram remember: malformed --extra-trigger {spec!r}")
    return out


def _insert(
    *,
    conn,
    name: str,
    description: str,
    body: str,
    type_: str,
    scope: str,
    project_slug: str | None,
    pinned: bool,
    candidates: list[FormationCandidate],
    extra_triggers: list[dict[str, Any]],
) -> int:
    now_ts = int(time.time())
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO memories "
            "(name, description, body, type, scope, project_slug, created_ts, pinned) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, description, body, type_, scope, project_slug, now_ts, 1 if pinned else 0),
        )
        memory_id = int(cur.lastrowid)
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
    type_: str,
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
            "type": type_,
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
        "tool_name": c.tool_name,
        "head": list(c.head) if c.head else None,
        "path_pattern": c.path_pattern,
        "source": c.source,
        "existing_memories": c.existing_memories,
    }


if __name__ == "__main__":
    raise SystemExit(main())
