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
    insert_candidate_triggers,
)
from .pretool import slugify_cwd

VALID_TYPES = {"feedback", "reference"}
VALID_SCOPES = {"global", "project"}
DEFAULT_TYPE = "reference"
DEFAULT_SCOPE = "project"

_NAME_MAX = 80

# Dedup: if an existing memory shares this many triggers with the new one,
# update instead of insert. Also updates if normalized names match + 1 overlap.
# Set to 1 because we suppress head-1 for subcommand tools (git push emits
# only (git, push), not (git) + (git, push)).
_DEDUP_TRIGGER_THRESHOLD = 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    body = _read_body(args)
    if not body:
        print("engram remember: body is empty (pass text as arg, -, or pipe stdin)", file=sys.stderr)
        return 2

    if args.type not in VALID_TYPES:
        print(f"engram remember: --type must be one of {sorted(VALID_TYPES)}", file=sys.stderr)
        return 2
    if args.scope not in VALID_SCOPES:
        print(f"engram remember: --scope must be one of {sorted(VALID_SCOPES)}", file=sys.stderr)
        return 2

    name = args.name or _synthesize_name(body)
    description = args.description or ""

    extra_triggers = _parse_extra_triggers(args.extra_trigger or [])

    conn = db.connect()
    try:
        candidates = extract_candidates(body)
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

        # --- Gate 1: reject triggerless memories ---
        if not all_triggers:
            print(json.dumps({
                "error": "no_triggers",
                "message": (
                    "No tool-call triggers could be extracted from the body. "
                    "Include backticked commands (e.g. `git push`, `mycli -c`) "
                    "or file paths so the memory has something to bind to. "
                    "A memory without triggers will never surface."
                ),
                "body_preview": body[:200],
            }))
            return 1

        candidates = consolidate_vocabulary(conn, candidates)

        # --- Gate 2: dedup check ---
        project_slug = _resolve_project_slug(args.scope, args.project_slug)
        existing = _find_overlapping_memory(conn, name, all_triggers, project_slug)

        if args.dry_run:
            payload = _build_payload(
                memory_id=None,
                name=name,
                description=description,
                body=body,
                type_=args.type,
                scope=args.scope,
                project_slug=project_slug,
                pinned=args.pinned,
                candidates=candidates,
                extra_triggers=extra_triggers,
                action="dry_run",
                existing_match=existing,
            )
            print(json.dumps(payload, indent=2))
            return 0

        if existing:
            memory_id = _update_existing(
                conn=conn,
                existing_id=existing["id"],
                name=name,
                description=description,
                body=body,
                type_=args.type,
                pinned=args.pinned,
                candidates=candidates,
                extra_triggers=extra_triggers,
            )
            payload = _build_payload(
                memory_id=memory_id,
                name=name,
                description=description,
                body=body,
                type_=args.type,
                scope=args.scope,
                project_slug=project_slug,
                pinned=args.pinned,
                candidates=candidates,
                extra_triggers=extra_triggers,
                action="updated",
                existing_match=existing,
            )
        else:
            memory_id = _insert(
                conn=conn,
                name=name,
                description=description,
                body=body,
                type_=args.type,
                scope=args.scope,
                project_slug=project_slug,
                pinned=args.pinned,
                candidates=candidates,
                extra_triggers=extra_triggers,
            )
            payload = _build_payload(
                memory_id=memory_id,
                name=name,
                description=description,
                body=body,
                type_=args.type,
                scope=args.scope,
                project_slug=project_slug,
                pinned=args.pinned,
                candidates=candidates,
                extra_triggers=extra_triggers,
                action="inserted",
                existing_match=None,
            )

        print(json.dumps(payload))
        return 0
    finally:
        conn.close()


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
    parser.add_argument("--extra-trigger", action="append", default=None,
                        metavar="SPEC",
                        help=("Extra trigger. Repeatable. Formats: "
                              "'tool_head:Bash:git,push'  |  "
                              "'path_glob:**/*.py'"))
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


# ---------- extra triggers ----------


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


# ---------- dedup ----------


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    import re
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _find_overlapping_memory(
    conn,
    name: str,
    candidates: list[FormationCandidate],
    project_slug: str | None,
) -> dict | None:
    """Find an existing non-archived memory that overlaps with the new one.

    Returns the best match as a dict {id, name, overlap_count, match_reason}
    or None if no strong overlap.
    """
    norm_name = _normalize_name(name)

    # Collect trigger signatures from candidates.
    new_heads = set()
    new_globs = set()
    for c in candidates:
        if c.kind == "tool_head" and c.head:
            new_heads.add((c.tool_name or "", " ".join(c.head)))
        elif c.kind == "path_glob" and c.path_pattern:
            new_globs.add(c.path_pattern)

    if not new_heads and not new_globs:
        return None

    # Query existing memories + their triggers.
    rows = conn.execute(
        "SELECT m.id, m.name, t.kind, t.tool_name, t.head_joined, t.path_pattern "
        "FROM memories m JOIN triggers t ON t.memory_id = m.id "
        "WHERE m.archived_ts IS NULL "
        "AND (m.scope = 'global' OR m.project_slug = ?)",
        (project_slug,),
    ).fetchall()

    # Score each existing memory by trigger overlap.
    scores: dict[int, dict] = {}
    for row in rows:
        mid = row["id"]
        if mid not in scores:
            scores[mid] = {"id": mid, "name": row["name"], "overlap": 0, "reason": []}

        if row["kind"] == "tool_head":
            key = (row["tool_name"] or "", row["head_joined"] or "")
            if key in new_heads:
                scores[mid]["overlap"] += 1
                scores[mid]["reason"].append(f"tool_head:{key[1]}")
        elif row["kind"] == "path_glob":
            if row["path_pattern"] in new_globs:
                scores[mid]["overlap"] += 1
                scores[mid]["reason"].append(f"path_glob:{row['path_pattern']}")

    # Find best match.
    best = None
    for s in scores.values():
        # Strong overlap: ≥ threshold shared triggers
        if s["overlap"] >= _DEDUP_TRIGGER_THRESHOLD:
            if best is None or s["overlap"] > best["overlap"]:
                best = s
        # Or: same normalized name with any trigger overlap
        elif s["overlap"] >= 1 and _normalize_name(s["name"]) == norm_name:
            s["reason"].append("name_match")
            if best is None or s["overlap"] > best["overlap"]:
                best = s

    if best:
        return {
            "id": best["id"],
            "name": best["name"],
            "overlap_count": best["overlap"],
            "match_reason": ", ".join(best["reason"]),
        }
    return None


# ---------- insert / update ----------


def _update_existing(
    *,
    conn,
    existing_id: int,
    name: str,
    description: str,
    body: str,
    type_: str,
    pinned: bool,
    candidates: list[FormationCandidate],
    extra_triggers: list[dict[str, Any]],
) -> int:
    """Replace body/name/type on an existing memory, merge triggers."""
    now_ts = int(time.time())
    with db.transaction(conn):
        conn.execute(
            "UPDATE memories SET name = ?, description = ?, body = ?, type = ?, "
            "pinned = ?, created_ts = ? WHERE id = ?",
            (name, description, body, type_, 1 if pinned else 0, now_ts, existing_id),
        )
        # Remove old triggers and replace with new extraction.
        conn.execute("DELETE FROM triggers WHERE memory_id = ?", (existing_id,))
        insert_candidate_triggers(conn, existing_id, candidates)
        _insert_extra_triggers(conn, existing_id, extra_triggers)
    return existing_id


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
        _insert_extra_triggers(conn, memory_id, extra_triggers)
    return memory_id


def _insert_extra_triggers(conn, memory_id: int, extras: list[dict[str, Any]]) -> None:
    """Mirrors seed._insert_triggers; kept local so remember.py owns its own schema writes."""
    for t in extras:
        kind = t["kind"]
        if kind == "tool_head":
            head = t["head"]
            conn.execute(
                "INSERT INTO triggers "
                "(memory_id, kind, tool_name, head_joined, head_length) "
                "VALUES (?, 'tool_head', ?, ?, ?)",
                (memory_id, t["tool_name"], " ".join(head), len(head)),
            )
        elif kind == "path_glob":
            conn.execute(
                "INSERT INTO triggers (memory_id, kind, path_pattern) "
                "VALUES (?, 'path_glob', ?)",
                (memory_id, t["path_pattern"]),
            )


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
