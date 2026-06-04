"""Shared utility functions."""

from __future__ import annotations

import os
from pathlib import Path

# Env var set on each detached watcher-tick by spawn_tick. Any `claude` the
# tick launches inherits it, so the SessionStart / UserPromptSubmit hooks
# running inside that child can refuse to spawn yet another watcher. This is
# belt-and-suspenders on top of `--bare`: if `--bare` ever stops suppressing
# hooks (the May-2026 recursive-spawn burst), this still stops the recursion.
WATCHER_CHILD_ENV = "ENGRAM_IN_WATCHER"


def is_watcher_child() -> bool:
    """True if this process was spawned by (or inside) the watcher subprocess."""
    return os.environ.get(WATCHER_CHILD_ENV) == "1"


def slugify_cwd(cwd: str) -> str:
    """Match Claude Code's project-slug convention: `/` → `-`."""
    return cwd.replace("/", "-")


def unslugify_candidates(slug: str) -> list[Path]:
    """Enumerate candidate paths that could have produced this Claude Code slug.

    `slugify_cwd` is lossy: directory names containing `-` (like `tool-engrams`)
    are indistinguishable from path separators after slugification. This walks
    the slug and yields every plausible `/`-split, then keeps only those that
    exist on disk. Returns paths in length order (deepest first) so callers
    that just want "the best guess" can take [0].

    Example:
        unslugify_candidates("-Users-jpcar-personal-projects-tool-engrams")
        -> [PosixPath('/Users/jpcar/personal-projects/tool-engrams'), ...]
    """
    if not slug or not slug.startswith("-"):
        return []
    # Strip leading `-` (the original `/`), then split on each remaining `-`.
    tokens = slug[1:].split("-")
    candidates: list[Path] = []
    # Try every contiguous grouping: each `-` is either a `/` or a literal `-`.
    # For N tokens there are 2^(N-1) groupings — but realistic slugs have
    # <12 tokens so 4096 paths max, each a quick exists() check.
    n = len(tokens)
    if n == 0:
        return []
    for mask in range(1 << (n - 1)):
        parts: list[str] = [tokens[0]]
        for i in range(n - 1):
            if mask & (1 << i):
                parts[-1] = parts[-1] + "-" + tokens[i + 1]
            else:
                parts.append(tokens[i + 1])
        path = Path("/" + "/".join(parts))
        if path.is_dir():
            candidates.append(path)
    # Deepest match first so caller-prefers-longest behavior is natural.
    candidates.sort(key=lambda p: -len(p.parts))
    return candidates


def is_pid_alive(pid: int) -> bool:
    """Check if a process with this PID is still running.

    Uses signal 0 — POSIX trick for "exists?" with no actual signal sent.
    Returns False on any OSError (no such process, no permission, etc.).
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
