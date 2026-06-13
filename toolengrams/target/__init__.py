"""Target adapters — the hooked coding-agent harnesses memories surface into.

See interface.py for the contract. Unlike the engine (one active choice),
several targets can be wired at once; each wired hook command carries its
target via `--target`, defaulting to claude-code so pre-seam installs keep
working unchanged.
"""

from __future__ import annotations

import sys

from ..harness_names import CLAUDE_CODE
from .interface import SessionFile, TargetAdapter
from . import claude_code, codex

TARGETS = {
    claude_code.NAME: claude_code,
    codex.NAME: codex,
}


def get_target(name: str | None = None):
    """Resolve a target adapter by name. Unknown or missing names warn on
    stderr and fall back to claude-code — hooks are fail-open; a typo'd
    --target must degrade, never break a tool call."""
    if not name:
        return TARGETS[CLAUDE_CODE]
    target = TARGETS.get(name)
    if target is None:
        print(f"engram: unknown target {name!r}; falling back to {CLAUDE_CODE}",
              file=sys.stderr)
        return TARGETS[CLAUDE_CODE]
    return target


__all__ = ["TARGETS", "TargetAdapter", "SessionFile", "get_target"]
