"""Shared helpers for hook handlers.

  - `is_internal_cwd` is used by session_start.py and user_prompt.py to avoid
    spawning watchers for ToolEngrams' own subprocess sessions (the
    consolidation agent's claude -p in a temp dir, etc.). Without this, the
    consolidation agent's own transcript gets watched, which caused big
    irrelevant transcripts to hit the 60s watcher-model timeout.

  - `WHITELIST` is the set of tool names whose calls carry user-facing memory
    bindings — shared by pretool.py and post_tool_failure.py so we never
    drift on which tools surface memories.

  - `max_memories_per_call()` returns the per-call cap on injected memories.
    Char-bounded truncation downstream still kicks in, but a hard count cap
    prevents a noisy first-token bucket from spraying Claude's context.
"""

from __future__ import annotations

import os

# Tools whose pre/post-failure events trigger memory surfacing. New tools
# added to Claude Code that should bind memories go here, not in two places.
WHITELIST: frozenset[str] = frozenset({
    "Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob",
    "WebFetch", "NotebookEdit",
})

DEFAULT_MAX_MEMORIES_PER_CALL = 2
# Hard ceiling defends against typo overrides like ENGRAM_MAX_MEMORIES_PER_CALL=200
# silently un-capping the system.
MAX_MEMORIES_PER_CALL_CEILING = 10


def max_memories_per_call() -> int:
    """Per-call ceiling on surfaced memories. Override via $ENGRAM_MAX_MEMORIES_PER_CALL."""
    raw = os.environ.get("ENGRAM_MAX_MEMORIES_PER_CALL")
    if raw is None:
        return DEFAULT_MAX_MEMORIES_PER_CALL
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_MAX_MEMORIES_PER_CALL
    return max(1, min(n, MAX_MEMORIES_PER_CALL_CEILING))

# Temp dir basenames that identify non-user (ToolEngrams-internal) sessions.
# Match by prefix on the cwd basename.
_INTERNAL_CWD_PREFIXES: tuple[str, ...] = (
    "engram-consolidate-",
    "engram-formation-",
    "engram-eval-",
    "engram-observe-",
    "engram-experiment-",
)


def is_internal_cwd(cwd: str) -> bool:
    """True if the session's cwd is one of our own temp dirs."""
    if not cwd:
        return False
    basename = cwd.rstrip("/").rsplit("/", 1)[-1] if "/" in cwd else cwd
    return any(basename.startswith(p) for p in _INTERNAL_CWD_PREFIXES)
