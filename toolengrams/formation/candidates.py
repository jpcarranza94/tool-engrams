"""Formation-time trigger extraction from memory body text.

The design (docs/design-v9.md §3) says `engram remember` should deterministically
parse the body for patterns that bind the memory to future tool calls:

  - Backticked shell snippets → token_subseq triggers
  - Tilde / absolute / repo-rooted paths → path_glob triggers
  - URL hosts → token_subseq triggers (host as the first token)
  - Bare CLI names mentioned in prose → token_subseq (single-token) triggers

Then it consolidates against existing vocabulary: candidates whose pattern
already exists on ≥1 other memory get annotated with the match count so
upstream can prefer convergence over sprouting new clusters.

This module is pure extraction + annotation; it never touches the DB by
itself. `cli/remember.py` wires it to persistence.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Literal

from ..retrieval.extract import _SUBCOMMAND_TOOLS, _tokenize_bash

CandidateKind = Literal["token_subseq", "path_glob"]

# Backticked shell snippet. Single-line only — we don't try to parse fenced blocks.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# URL extraction. Captures host + path so we can peel off the host.
_URL_RE = re.compile(r"https?://([^\s`'\"()<>]+)")

# Tilde / absolute / dotted-relative paths that look like filesystem refs.
_PATH_RE = re.compile(
    r"(?<![/\w])("
    r"~(?:/[A-Za-z0-9_./\-]+)?"          # ~/foo/bar
    r"|/[A-Za-z0-9_][A-Za-z0-9_./\-]*"   # /abs/path
    r"|\./[A-Za-z0-9_][A-Za-z0-9_./\-]*" # ./rel/path
    r"|\*\*/[A-Za-z0-9_.\-*]+"            # **/glob
    r")"
)

# A token that looks like a CLI name (starts with a letter, no shell metachars).
_CLI_NAME_RE = re.compile(r"^[a-zA-Z_][\w.-]*$")


@dataclass(slots=True)
class FormationCandidate:
    """A single candidate trigger extracted from a memory body."""

    kind: CandidateKind
    tokens: tuple[str, ...] = ()
    path_pattern: str | None = None
    source: str = ""                       # "backtick" | "path" | "url" | "extra" | "explicit"
    existing_memories: int = 0             # set by consolidate_vocabulary

    @property
    def dedup_key(self) -> tuple:
        return (self.kind, self.tokens, self.path_pattern)


# --------- extraction ---------


def extract_candidates(body: str) -> list[FormationCandidate]:
    """Parse the body and return a deduplicated list of candidate triggers."""
    out: list[FormationCandidate] = []
    seen: set[tuple] = set()

    def _add(c: FormationCandidate) -> None:
        if c.dedup_key in seen:
            return
        seen.add(c.dedup_key)
        out.append(c)

    _extract_backticks(body, _add)
    _extract_paths(body, _add)
    _extract_urls(body, _add)

    return out


def _extract_backticks(body: str, add) -> None:
    for match in _BACKTICK_RE.finditer(body):
        snippet = match.group(1).strip()
        if not snippet:
            continue

        # Skip backticks that obviously wrap a path, a flag, or a variable.
        if snippet.startswith(("/", "~", ".", "-", "$")):
            continue

        tokens = _tokenize_bash(snippet)
        if not tokens:
            continue

        head1 = tokens[0]
        if not _CLI_NAME_RE.match(head1):
            continue

        # For subcommand tools (git, aws, docker, etc.), prefer the more
        # specific two-token trigger. Only emit one-token if no two-token exists.
        emitted_two = False
        if head1 in _SUBCOMMAND_TOOLS and len(tokens) >= 2:
            head2 = tokens[1]
            if _CLI_NAME_RE.match(head2):
                add(FormationCandidate(
                    kind="token_subseq",
                    tokens=(head1, head2),
                    source="backtick",
                ))
                emitted_two = True

        if not emitted_two:
            add(FormationCandidate(
                kind="token_subseq",
                tokens=(head1,),
                source="backtick",
            ))


def _extract_paths(body: str, add) -> None:
    for match in _PATH_RE.finditer(body):
        path = match.group(1).rstrip(".,;:)]}'\"")
        if not path or len(path) < 2:
            continue

        # Full path as-is.
        add(FormationCandidate(
            kind="path_glob",
            path_pattern=path,
            source="path",
        ))

        # Also emit a **/<basename> glob so the memory fires from any cwd.
        if "/" in path and not path.startswith("**/"):
            basename = path.rstrip("/").rsplit("/", 1)[-1]
            if (
                basename
                and "*" not in basename
                and ("." in basename or len(basename) > 2)
            ):
                add(FormationCandidate(
                    kind="path_glob",
                    path_pattern=f"**/{basename}",
                    source="path",
                ))


def _extract_urls(body: str, add) -> None:
    for match in _URL_RE.finditer(body):
        host_path = match.group(1)
        host = host_path.split("/", 1)[0].rstrip(".,;:)]}'\"")
        if host:
            add(FormationCandidate(
                kind="token_subseq",
                tokens=(host,),
                source="url",
            ))


# --------- consolidation ---------


def consolidate_vocabulary(
    conn: sqlite3.Connection,
    candidates: Iterable[FormationCandidate],
) -> list[FormationCandidate]:
    """Annotate each candidate with how many existing memories share the same trigger.

    This is the "convergence by gravity" step from the design doc: we don't
    *drop* candidates with zero existing matches (new vocabulary has to start
    somewhere), but the count lets upstream prefer established patterns.
    """
    out = list(candidates)
    for c in out:
        if c.kind == "token_subseq":
            tokens_json = json.dumps(list(c.tokens))
            row = conn.execute(
                "SELECT COUNT(DISTINCT memory_id) FROM triggers "
                "WHERE kind = 'token_subseq' AND tokens_json = ?",
                (tokens_json,),
            ).fetchone()
        elif c.kind == "path_glob":
            row = conn.execute(
                "SELECT COUNT(DISTINCT memory_id) FROM triggers "
                "WHERE kind = 'path_glob' AND path_pattern = ?",
                (c.path_pattern,),
            ).fetchone()
        else:
            row = (0,)
        c.existing_memories = int(row[0] or 0)
    return out
