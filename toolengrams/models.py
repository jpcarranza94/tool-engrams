"""Dataclasses for in-memory domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MemoryKind = Literal["block", "hint"]
MemoryScope = Literal["global", "project"]
TriggerKind = Literal["token_subseq", "path_glob"]
HookName = Literal[
    "pre_tool_use",
    "post_tool_use",
    "session_start",
]


@dataclass(slots=True)
class Memory:
    id: int | None
    name: str
    description: str
    body: str
    kind: MemoryKind
    scope: MemoryScope
    project_slug: str | None
    created_ts: int
    last_surfaced_ts: int = 0
    surface_count: int = 0
    useful_count: int = 0
    pinned: bool = False
    archived_ts: int | None = None


@dataclass(slots=True)
class Trigger:
    id: int | None
    memory_id: int
    kind: TriggerKind
    first_token: str | None = None
    tokens: tuple[str, ...] = ()
    path_pattern: str | None = None


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
class SessionSurface:
    session_id: str
    memory_id: int
    surfaced_ts: int
    hook: HookName
    tool_use_id: str | None = None


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
