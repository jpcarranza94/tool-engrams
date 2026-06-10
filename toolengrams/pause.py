"""Kill switch — `engram pause` / `engram resume` + $ENGRAM_DISABLED.

One command stops everything: surfacing, watcher ticks, background spend.
The flag file lives next to the DB so it is trivially inspectable and shared
by every entry point. All hook handlers and the watcher tick call
`is_disabled()` first and stand down fail-open (exit 0, empty output,
no DB touch, no model calls).

Precedence: $ENGRAM_DISABLED beats the flag file. "1"/"true"/"yes"
force-disables; "0"/"false"/"no" force-enables (scripting/CI override);
unset falls back to the flag file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import db

FLAG_NAME = "paused"

_ENV_FALSE = {"0", "false", "no"}


def flag_path() -> Path:
    return db.db_path().parent / FLAG_NAME


def is_disabled() -> bool:
    """True when the whole system should stand down. Never raises."""
    try:
        env = os.environ.get("ENGRAM_DISABLED")
        if env is not None and env.strip():
            return env.strip().lower() not in _ENV_FALSE
        return flag_path().exists()
    except Exception:
        return False


def run_pause() -> int:
    """CLI: `engram pause` — create the flag file."""
    path = flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"paused at {int(time.time())}\n")
    print(json.dumps({
        "action": "paused",
        "flag": str(path),
        "message": "ToolEngrams is off: no surfacing, no watcher ticks, no spend. "
                   "Run 'engram resume' to turn it back on.",
    }))
    return 0


def run_resume() -> int:
    """CLI: `engram resume` — remove the flag file."""
    path = flag_path()
    existed = path.exists()
    if existed:
        path.unlink()
    payload = {"action": "resumed", "flag": str(path), "was_paused": existed}
    if os.environ.get("ENGRAM_DISABLED"):
        payload["warning"] = ("ENGRAM_DISABLED is set in the environment and "
                              "overrides the flag file; unset it to fully resume.")
    print(json.dumps(payload))
    return 0
