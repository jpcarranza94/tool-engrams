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
from ..retrieval.extract import _SUBCOMMAND_TOOLS
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

# Extension-only glob: `**/*.py`, `**/*.json`, or the bare `*.py` form. Binds a
# memory to *every* file of a type across every repo — pure noise.
_EXT_ONLY_GLOB_RE = re.compile(r"^(?:\*\*/)?\*\.[A-Za-z0-9]+$")

# Match-anything globs.
_MATCH_ALL_GLOBS = frozenset({"**", "**/*", "*"})

# Basenames so common that `**/<name>` fires in nearly every repo. A bare
# `**/<common-basename>` glob is refused at formation; a *directory-qualified*
# glob (`**/billing/models.py`) is fine because the directory narrows it.
_COMMON_BASENAMES = frozenset({
    "__init__.py", "main.py", "models.py", "utils.py", "config.py",
    "settings.py", "conftest.py", "setup.py", "index.js", "index.ts",
    "package.json", "tsconfig.json", "config.json", "settings.json",
    "config.yml", "config.yaml", "docker-compose.yml", "dockerfile",
    "makefile", "readme.md", "changelog.md", ".env", ".gitignore",
})


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


def token_trigger_is_specific_enough(tokens: tuple[str, ...]) -> bool:
    """Reject a `token_subseq` trigger that is too broad to bind usefully.

    Enforces the watcher.md rule "each trigger phrase must have 2+ words" for
    the CLIs where a bare command name over-matches: a single-token trigger on a
    **subcommand tool** (`git`, `gh`, `jira`, `ssh`, … — see `_SUBCOMMAND_TOOLS`)
    fires on *every* invocation of that tool, so it needs at least the
    subcommand too. Single-token triggers stay legal for simple no-subcommand
    tools (`ergdb`, `curl`) and for URL hosts (`openai.com`), which don't
    over-match. Two-or-more-token triggers always pass (the subcommand narrows
    them). Go-forward only: existing triggers are untouched at match time.
    """
    if len(tokens) >= 2:
        return True
    if not tokens:
        return False
    return tokens[0] not in _SUBCOMMAND_TOOLS


def path_glob_is_specific_enough(pattern: str) -> bool:
    """Reject a `path_glob` trigger that matches far too broadly.

    Three refusals: match-anything globs (`**`, `**/*`), extension-only globs
    (`**/*.py` — every file of a type in every repo), and a bare
    `**/<common-basename>` glob (`**/__init__.py`, `**/settings.json`) whose
    basename collides across nearly every project. A directory-qualified glob
    (`**/billing/models.py`) or an exact/rooted path is fine.
    """
    pat = pattern.strip()
    if not pat:
        return False
    if pat in _MATCH_ALL_GLOBS:
        return False
    if _EXT_ONLY_GLOB_RE.match(pat):
        return False
    if pat.startswith("**/"):
        rest = pat[3:]
        if "/" not in rest and rest.lower() in _COMMON_BASENAMES:
            return False
    return True


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
            if not token_trigger_is_specific_enough(tokens):
                print(
                    f"engram: rejected trigger for memory {memory_id} — "
                    f"single-token trigger {tokens[0]!r} is too broad; {tokens[0]!r} "
                    f"takes a subcommand, so the trigger needs 2+ tokens "
                    f"(tokens={list(tokens)})",
                    file=sys.stderr,
                )
                continue
            memory_store.add_token_trigger(conn, memory_id, tokens)
        elif c.kind == "path_glob":
            if not c.path_pattern:
                continue
            if not path_glob_is_specific_enough(c.path_pattern):
                print(
                    f"engram: rejected trigger for memory {memory_id} — "
                    f"path glob {c.path_pattern!r} is too broad to bind a memory; "
                    f"qualify it with a directory segment (e.g. "
                    f"'**/billing/models.py') so it doesn't match across every repo",
                    file=sys.stderr,
                )
                continue
            memory_store.add_path_trigger(conn, memory_id, c.path_pattern, c.access_mode)
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
