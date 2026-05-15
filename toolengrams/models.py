"""Dataclasses for in-memory domain objects.

Only objects actually constructed in Python live here. Rows from `memories`
and `triggers` are passed around as `sqlite3.Row` directly (cheaper, less
ceremony for read-only data); see retrieval/rank.py for examples.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MemoryKind = Literal["block", "hint"]
MemoryScope = Literal["global", "project"]


@dataclass(slots=True)
class Candidate:
    """A memory retrieved as a trigger match, pre-ranking."""

    memory_id: int
    name: str
    body: str
    matched_tokens: tuple[str, ...]
    matched_path: str | None
    surface_count: int
    useful_count: int
    last_surfaced_ts: int
    pinned: bool
    kind: MemoryKind
    scope: MemoryScope
    structural_match: float = 1.0
    final_score: float = 0.0

    @property
    def first_token(self) -> str:
        return self.matched_tokens[0] if self.matched_tokens else ""


@dataclass(slots=True)
class ExtractedTriggerHint:
    """Output of extraction: what a tool-call payload produces as lookup keys.

    `tokens` is the tokenization of the call itself — for Bash, the shell
    tokens; for WebFetch, host + URL path segments. The first token is used
    as the indexed lookup key; the full list is subsequence-matched against
    stored trigger tokens.

    `paths` feeds path-glob matching for file-centric tools (Read/Edit/etc).
    """

    tool_name: str
    tokens: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
