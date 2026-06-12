"""Format-agnostic transcript I/O shared by every target's parser."""

from __future__ import annotations


def _read_lines_from(path: str, start_line: int) -> list[str]:
    """Read JSONL lines from start_line to EOF."""
    try:
        with open(path) as f:
            lines = f.readlines()
        return lines[start_line:]
    except (FileNotFoundError, OSError):
        return []
