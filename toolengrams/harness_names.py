"""Shared harness name constants.

ToolEngrams talks to coding-agent harnesses in two distinct roles:

- **engine** — the headless runner for background LLM work (watcher
  formation/eval ticks, nightly consolidation). One active choice.
- **target** — the harness whose tool calls we hook (memories surface into
  it). Not exclusive: several targets can be wired against one DB.

One tiny leaf module so the engine/ and target/ packages never have to
import each other for a name.
"""

from __future__ import annotations

CLAUDE_CODE = "claude-code"
CODEX = "codex"
