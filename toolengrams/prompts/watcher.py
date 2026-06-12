"""Watcher prompt — resolves via prompts/loader.py.

Lookup order:
    1. $ENGRAM_WATCHER_PROMPT_PATH
    2. <engram home>/prompts/watcher.md
    3. toolengrams/prompts/defaults/watcher.md
"""

from .loader import load_prompt


def build_watcher_prompt(cwd: str = "") -> str:
    """Formation prompt, with the user's cwd interpolated so the model passes it
    to `engram remember --project-cwd`."""
    return load_prompt("watcher", cwd=cwd)

