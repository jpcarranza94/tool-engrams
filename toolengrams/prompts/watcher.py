"""Watcher prompt — resolves via prompts/loader.py.

Lookup order:
    1. $ENGRAM_WATCHER_PROMPT_PATH
    2. ~/.claude/tool-engrams/prompts/watcher.md
    3. toolengrams/prompts/defaults/watcher.md
"""

from .loader import load_prompt


def build_watcher_prompt(cwd: str = "") -> str:
    """Formation prompt, with the user's cwd interpolated so the model passes it
    to `engram remember --project-cwd`."""
    return load_prompt("watcher", cwd=cwd)


# Header prepended to the delta on a resumed formation session (the standing
# guidance already lives in the session history; each pass just sends new lines).
WATCHER_SUBSEQUENT_HEADER = "--- New activity ---\n\n"
