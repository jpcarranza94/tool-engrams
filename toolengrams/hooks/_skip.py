"""Shared helpers for hook handlers.

`is_internal_cwd` is used by both session_start.py and user_prompt.py to
avoid spawning watchers for ToolEngrams' own subprocess sessions (the
consolidation agent's claude -p in a temp dir, etc.). Without this, the
consolidation agent's own transcript gets watched, which caused big
irrelevant transcripts to hit the 60s Haiku timeout.
"""

from __future__ import annotations

# Temp dir basenames that identify non-user (ToolEngrams-internal) sessions.
# Match by prefix on the cwd basename.
_INTERNAL_CWD_PREFIXES: tuple[str, ...] = (
    "engram-consolidate-",
    "engram-observe-",
    "engram-experiment-",
)


def is_internal_cwd(cwd: str) -> bool:
    """True if the session's cwd is one of our own temp dirs."""
    if not cwd:
        return False
    basename = cwd.rstrip("/").rsplit("/", 1)[-1] if "/" in cwd else cwd
    return any(basename.startswith(p) for p in _INTERNAL_CWD_PREFIXES)
