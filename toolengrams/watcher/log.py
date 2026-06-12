"""Watcher log sink — the single place that owns the watcher log file.

A leaf module (no intra-package imports beyond the `paths` leaf) so both the
tick engine (`tick.py`) and the state store (`state.py`) can append without an
import cycle.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..paths import engram_home


def log_path() -> Path:
    """Resolved at call time so the whole seam shares one contract with
    db.db_path() — no import-order surprises around $ENGRAM_HOME."""
    return engram_home() / "watcher.log"


def _log(msg: str) -> None:
    """Append a timestamped line to the watcher log. Never raises."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass
