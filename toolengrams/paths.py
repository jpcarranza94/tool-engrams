"""Data-home resolution — the single seam for where ToolEngrams keeps state.

Everything ToolEngrams persists outside the repo lives under one home
directory: the sqlite DB, the watcher log, watcher sandboxes, the pause
flag, and per-user prompt overrides. Resolution order:

    1. $ENGRAM_HOME              explicit override
    2. ~/.tool-engrams           neutral default, when it already exists
    3. ~/.claude/tool-engrams    legacy home, when it already exists
    4. ~/.tool-engrams           fresh-install default

The legacy fallback keeps code shipped ahead of a re-install pointing at
existing data; install.sh migrates the legacy dir to the neutral default
and leaves a symlink behind. $ENGRAM_DB still overrides the DB *file*
independently — see db.db_path().

A leaf module (stdlib only) so hot-path hooks and the watcher's leaf log
sink can both import it without cycles.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = Path.home() / ".tool-engrams"
LEGACY_HOME = Path.home() / ".claude" / "tool-engrams"


def engram_home() -> Path:
    override = os.environ.get("ENGRAM_HOME")
    if override:
        return Path(override).expanduser()
    if DEFAULT_HOME.exists():
        return DEFAULT_HOME
    if LEGACY_HOME.exists():
        return LEGACY_HOME
    return DEFAULT_HOME
