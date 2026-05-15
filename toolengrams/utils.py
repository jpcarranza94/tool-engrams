"""Shared utility functions."""

from __future__ import annotations

import os


def slugify_cwd(cwd: str) -> str:
    """Match Claude Code's project-slug convention: `/` → `-`."""
    return cwd.replace("/", "-")


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
