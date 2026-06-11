"""Trigger persistence: write FormationCandidates to the triggers table.

Both dedup.py and cli/remember.py import from here. Storage shape:
  - token_subseq: first_token (indexed) + tokens_json (JSON array of tokens)
  - path_glob: path_pattern

Validates each candidate at the chokepoint — invalid trigger shapes that
can never match a real tool call (e.g. first_token = "STAGING_FOO=" or
"/abs/path") are dropped with a warning to stderr. See
`first_token_looks_like_cli` for the predicate.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from typing import Any, Iterable

from .. import memory_store
from .candidates import FormationCandidate

# A valid Bash first_token shape: letter/underscore start, then word chars,
# dots, or hyphens. Excludes anything that can never be a shell command head:
#   - flag fragments ("--foo")          → start with '-'
#   - absolute paths ("/opt/...")       → start with '/'
#   - relative paths (".claude/...")    → contains '/'
#   - env var assignments ("FOO=bar")   → contains '='
#   - whitespace                        → never a single shell token
#   - URL-like hosts ("openai.com")     → permitted (legitimate first_token
#     for WebFetch and URL-rooted triggers)
_VALID_FIRST_TOKEN_RE = re.compile(r"^[A-Za-z_][\w.-]*$")


def first_token_looks_like_cli(first_token: str | None) -> bool:
    """Predicate used by insert_candidate_triggers to reject malformed triggers.

    Real shell calls always start with a token matching this shape (a command
    name like `git`, `aws`, `ergdb`, or a host like `openai.com` for WebFetch).
    Triggers whose first_token doesn't fit can never fire — see audit findings
    in PR #20 description and the 'never-surfaced' memories with first_tokens
    `STAGING_CUSTOMER_ALLOWLIST=`, `/opt/agent-service/.env`, `.claude/skills/`.
    """
    if not first_token:
        return False
    return bool(_VALID_FIRST_TOKEN_RE.match(first_token))


def insert_candidate_triggers(
    conn: sqlite3.Connection,
    memory_id: int,
    candidates: Iterable[FormationCandidate],
) -> int:
    """Write candidates as rows in the triggers table. Returns the insert count.

    Drops candidates whose first_token is structurally impossible (see
    `first_token_looks_like_cli`). Emits one stderr line per drop so the
    watcher or user can spot bad output.
    """
    n = 0
    for c in candidates:
        if c.kind == "token_subseq":
            tokens = tuple(c.tokens)
            if not tokens:
                continue
            if not first_token_looks_like_cli(tokens[0]):
                print(
                    f"engram: rejected trigger for memory {memory_id} — "
                    f"first_token {tokens[0]!r} is not a valid shell command head "
                    f"(tokens={list(tokens)})",
                    file=sys.stderr,
                )
                continue
            memory_store.add_token_trigger(conn, memory_id, tokens)
        elif c.kind == "path_glob":
            if not c.path_pattern:
                continue
            memory_store.add_path_trigger(conn, memory_id, c.path_pattern)
        else:
            continue
        n += 1
    return n


def extras_to_candidates(extras: list[dict[str, Any]]) -> list[FormationCandidate]:
    """Convert legacy --extra-trigger dicts into FormationCandidates."""
    out: list[FormationCandidate] = []
    for t in extras:
        kind = t.get("kind")
        if kind == "token_subseq":
            out.append(FormationCandidate(
                kind="token_subseq",
                tokens=tuple(t.get("tokens") or ()),
                source="extra",
            ))
        elif kind == "path_glob":
            out.append(FormationCandidate(
                kind="path_glob",
                path_pattern=t.get("path_pattern"),
                source="extra",
            ))
    return out
