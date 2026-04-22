"""Watcher prompt — resolves via prompts/loader.py.

Lookup order:
    1. $ENGRAM_WATCHER_PROMPT_PATH
    2. ~/.claude/tool-engrams/prompts/watcher.md
    3. toolengrams/prompts/defaults/watcher.md
"""

from .loader import load_prompt


def build_watcher_prompt() -> str:
    return load_prompt("watcher")


# Kept as a module attribute for back-compat with any inline readers, but the
# canonical accessor is build_watcher_prompt() so user overrides apply.
WATCHER_SUBSEQUENT_HEADER = "--- New activity (last 5 minutes) ---\n\n"
