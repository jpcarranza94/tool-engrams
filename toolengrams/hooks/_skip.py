"""Shared helpers for hook handlers.

  - `is_internal_cwd` is used by session_start.py and user_prompt.py to avoid
    spawning watchers for ToolEngrams' own subprocess sessions (the
    consolidation agent's claude -p in a temp dir, etc.). Without this, the
    consolidation agent's own transcript gets watched, which caused big
    irrelevant transcripts to hit the 60s watcher-model timeout.

  - `max_memories_per_call()` returns the per-call cap on injected memories.
    Char-bounded truncation downstream still kicks in, but a hard count cap
    prevents a noisy first-token bucket from spraying Claude's context.

  - `rank_and_cap()` is the single ordering+cap used by BOTH surfacing hooks
    (pretool.py and _failure_surface.py). It lived duplicated in the two, so
    a fix to one silently left the other starving proven memories.

  - `surface_notice()` is the $ENGRAM_SURFACE_NOTICE-gated systemMessage line
    pretool.py and post_tool_failure.py attach when memories surface.
"""

from __future__ import annotations

import os

DEFAULT_MAX_MEMORIES_PER_CALL = 4
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


def rank_and_cap(candidates: list) -> list:
    """Order candidates for injection and apply the per-call cap.

    Quality first, specificity only as the tiebreaker: a proven memory must
    not lose its slot to an unproven one that merely carries a longer trigger
    (path_glob candidates have no matched tokens at all, so under the old
    length-first key they sorted dead last unconditionally).

    Blocks are kept unconditionally and always ordered first — the deny path
    must never be diluted, and the char budget downstream drops the tail.
    Only hints are trimmed.
    """
    ranked = sorted(candidates, key=lambda c: (-c.final_score, -len(c.matched_tokens)))
    blocks = [c for c in ranked if c.kind == "block"]
    hints = [c for c in ranked if c.kind == "hint"]
    return blocks + hints[: max(max_memories_per_call() - len(blocks), 0)]


_NOTICE_TRUE = {"1", "true", "yes"}


def surface_notice(names: list[str]) -> str | None:
    """User-visible one-liner for the hook's systemMessage when memories
    surface, gated by $ENGRAM_SURFACE_NOTICE. Off by default — injection is
    deliberately invisible; this exists so the post-install smoke test (and
    anyone debugging surfacing) can SEE a memory fire in the transcript."""
    if os.environ.get("ENGRAM_SURFACE_NOTICE", "").strip().lower() not in _NOTICE_TRUE:
        return None
    if not names:
        return None
    listed = ", ".join(f"'{n}'" for n in names)
    return f"ToolEngrams surfaced: {listed}"


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
