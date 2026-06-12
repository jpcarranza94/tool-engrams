"""Prompt loader with user-override chain.

Lookup order (first match wins):
    1. $ENGRAM_{NAME_UPPER}_PROMPT_PATH  (explicit file override)
    2. <engram home>/prompts/{name}.md           (per-user override)
    3. toolengrams/prompts/defaults/{name}.md    (packaged default)

The engram home resolves via paths.engram_home() ($ENGRAM_HOME, then
~/.tool-engrams, then the legacy ~/.claude/tool-engrams).

Interpolation uses str.format — curly braces in the prompt itself must be
escaped by doubling (`{{` / `}}`). Variable names come from the caller.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..paths import engram_home

_DEFAULTS_DIR = Path(__file__).parent / "defaults"


def _user_override_dir() -> Path:
    """Resolved at call time so the whole seam shares one contract with
    db.db_path() — no import-order surprises around $ENGRAM_HOME."""
    return engram_home() / "prompts"


class PromptNotFound(RuntimeError):
    pass


def resolve_prompt_path(prompt_name: str) -> Path:
    """Return the path to the prompt file, in override-order.

    Raises PromptNotFound if no file is found (means the packaged default
    was deleted — a packaging bug).
    """
    env_key = f"ENGRAM_{prompt_name.upper()}_PROMPT_PATH"
    override = os.environ.get(env_key)
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p

    user = _user_override_dir() / f"{prompt_name}.md"
    if user.is_file():
        return user

    default = _DEFAULTS_DIR / f"{prompt_name}.md"
    if default.is_file():
        return default

    raise PromptNotFound(
        f"prompt '{prompt_name}' not found in $ENGRAM_{prompt_name.upper()}_PROMPT_PATH, "
        f"{user}, or packaged default at {default}"
    )


def load_prompt(prompt_name: str, /, **variables: object) -> str:
    """Load a prompt by name and interpolate variables via str.format.

    `prompt_name` is positional-only so variables named `name` don't collide.
    """
    path = resolve_prompt_path(prompt_name)
    template = path.read_text()
    if not variables:
        return template
    return template.format(**variables)
