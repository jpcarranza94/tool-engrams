"""Parse file paths out of Codex apply_patch envelopes."""

from __future__ import annotations

import re

_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (?P<path>.+?)\s*$"
)
_PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (?P<path>.+?)\s*$")


def paths_from_patch(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        match = _PATCH_FILE_RE.match(line) or _PATCH_MOVE_RE.match(line)
        if not match:
            continue
        path = match.group("path").strip()
        if path and path not in paths:
            paths.append(path)
    return paths
