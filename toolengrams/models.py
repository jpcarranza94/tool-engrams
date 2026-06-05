"""Dataclasses for in-memory domain objects.

`Memory` and `Trigger` are the domain model for the `memories` / `triggers`
tables; every read through `memory_store` returns these, so callers work with
typed fields instead of raw `sqlite3.Row` column strings. The hot PreToolUse
match path is the one deliberate exception — it stays on raw rows + the lean
`Candidate` to avoid per-call object allocation (see retrieval/rank.py).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

MemoryKind = Literal["block", "hint"]
MemoryScope = Literal["global", "project"]
TriggerKind = Literal["token_subseq", "path_glob"]


@dataclass(slots=True)
class Memory:
    """One row of the `memories` table."""

    id: int
    name: str
    description: str | None
    body: str
    kind: str
    scope: str
    project_slug: str | None
    created_ts: int
    last_surfaced_ts: int
    surface_count: int
    useful_count: int
    pinned: bool
    archived_ts: int | None
    last_verified_ts: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Memory":
        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            body=row["body"],
            kind=row["kind"],
            scope=row["scope"],
            project_slug=row["project_slug"],
            created_ts=row["created_ts"],
            last_surfaced_ts=row["last_surfaced_ts"],
            surface_count=row["surface_count"],
            useful_count=row["useful_count"],
            pinned=bool(row["pinned"]),
            archived_ts=row["archived_ts"],
            last_verified_ts=row["last_verified_ts"],
        )


@dataclass(slots=True)
class Trigger:
    """One row of the `triggers` table."""

    id: int
    memory_id: int
    kind: str
    first_token: str | None
    tokens_json: str | None
    path_pattern: str | None

    @property
    def tokens(self) -> list[str]:
        """Parsed token list for a token_subseq trigger (empty otherwise)."""
        if not self.tokens_json:
            return []
        try:
            parsed = json.loads(self.tokens_json)
        except (ValueError, TypeError):
            return []
        return [str(x) for x in parsed] if isinstance(parsed, list) else []

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Trigger":
        return cls(
            id=row["id"],
            memory_id=row["memory_id"],
            kind=row["kind"],
            first_token=row["first_token"],
            tokens_json=row["tokens_json"],
            path_pattern=row["path_pattern"],
        )


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
