"""Formation-time trigger extraction from memory body text.

The design (docs/design-v8.md §7) says `/remember` should deterministically
parse the body for patterns that bind the memory to future tool calls:

  - Backticked shell snippets → Bash tool_head triggers
  - Tilde / absolute / repo-rooted paths → path_glob triggers
  - URL hosts → WebFetch tool_head triggers
  - Bare CLI names mentioned in prose → Bash tool_head (single token only)

Then it consolidates against existing vocabulary: candidates whose pattern
already exists on ≥2 other memories get annotated with the match count so
upstream can prefer convergence over sprouting new clusters.

This module is pure extraction + annotation; it never touches the DB by
itself. `commands/remember.py` wires it to persistence.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Literal

from .extract import _SUBCOMMAND_TOOLS, _tokenize_bash

CandidateKind = Literal["tool_head", "path_glob"]

# Backticked shell snippet. Single-line only — we don't try to parse fenced blocks.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# URL extraction. Captures host + path so we can peel off the host.
_URL_RE = re.compile(r"https?://([^\s`'\"()<>]+)")

# Tilde / absolute / dotted-relative paths that look like filesystem refs.
# Intentionally narrower than extract._PATH_RE — we want things a human would
# typeset as a path, not every slash-containing token.
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
    tool_name: str | None = None          # "Bash" / "WebFetch" for tool_head, None for path_glob
    head: tuple[str, ...] = ()
    path_pattern: str | None = None
    source: str = ""                       # "backtick" | "path" | "url" | "cli-name"
    existing_memories: int = 0             # set by consolidate_vocabulary

    @property
    def dedup_key(self) -> tuple:
        return (self.kind, self.tool_name, self.head, self.path_pattern)


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

        add(FormationCandidate(
            kind="tool_head",
            tool_name="Bash",
            head=(head1,),
            source="backtick",
        ))

        if head1 in _SUBCOMMAND_TOOLS and len(tokens) >= 2:
            head2 = tokens[1]
            if _CLI_NAME_RE.match(head2):
                add(FormationCandidate(
                    kind="tool_head",
                    tool_name="Bash",
                    head=(head1, head2),
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
            if basename and "*" not in basename and "." in basename or len(basename) > 2:
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
                kind="tool_head",
                tool_name="WebFetch",
                head=(host,),
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
        if c.kind == "tool_head":
            row = conn.execute(
                "SELECT COUNT(DISTINCT memory_id) FROM triggers "
                "WHERE kind = 'tool_head' AND tool_name = ? AND head_joined = ?",
                (c.tool_name, " ".join(c.head)),
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


# --------- persistence helpers ---------


def insert_candidate_triggers(
    conn: sqlite3.Connection,
    memory_id: int,
    candidates: Iterable[FormationCandidate],
) -> int:
    """Write candidates as rows in the triggers table. Returns the insert count."""
    n = 0
    for c in candidates:
        if c.kind == "tool_head":
            head_joined = " ".join(c.head)
            conn.execute(
                "INSERT INTO triggers "
                "(memory_id, kind, tool_name, head_joined, head_length) "
                "VALUES (?, 'tool_head', ?, ?, ?)",
                (memory_id, c.tool_name, head_joined, len(c.head)),
            )
        elif c.kind == "path_glob":
            conn.execute(
                "INSERT INTO triggers (memory_id, kind, path_pattern) "
                "VALUES (?, 'path_glob', ?)",
                (memory_id, c.path_pattern),
            )
        else:
            continue
        n += 1
    return n
