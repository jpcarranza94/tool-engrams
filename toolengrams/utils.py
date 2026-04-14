"""Shared utility functions."""

from __future__ import annotations


def slugify_cwd(cwd: str) -> str:
    """Match Claude Code's project-slug convention: `/` → `-`."""
    return cwd.replace("/", "-")
