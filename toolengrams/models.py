"""Dataclasses for in-memory domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MemoryType = Literal["user", "feedback", "project", "reference"]
MemoryScope = Literal["global", "project"]
TriggerKind = Literal["tool_head", "path_glob"]
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
    type: MemoryType
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
    tool_name: str | None = None
    head_joined: str | None = None
    head_length: int | None = None
    path_pattern: str | None = None


@dataclass(slots=True)
class Candidate:
    """A memory retrieved as a trigger match, pre-ranking."""

    memory_id: int
    body: str
    name: str
    tool_name: str | None
    head_joined: str | None
    head_length: int
    surface_count: int
    useful_count: int
    last_surfaced_ts: int
    pinned: bool
    type: MemoryType
    scope: MemoryScope
    structural_match: float = 1.0
    final_score: float = 0.0


@dataclass(slots=True)
class ClusterStats:
    """Aggregate stats over a (tool_name, head_joined) bucket. Used by the Laplace threshold."""

    tool_name: str
    head_joined: str
    n_memories: int
    sum_final_score: float

    @property
    def mean_final_score(self) -> float:
        if self.n_memories == 0:
            return 0.0
        return self.sum_final_score / self.n_memories


@dataclass(slots=True)
class SessionSurface:
    session_id: str
    memory_id: int
    surfaced_ts: int
    hook: HookName
    tool_use_id: str | None = None


@dataclass(slots=True)
class ExtractedTriggerHint:
    """Output of extraction pass: what a PreToolUse payload produces as lookup keys."""

    tool_name: str
    head_prefixes: list[tuple[str, ...]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
