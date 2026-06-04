"""Watcher log sink — the single place that owns the watcher log file.

A leaf module (no intra-package imports beyond stdlib) so both the tick engine
(`tick.py`) and the state store (`state.py`) can append without an import cycle.
"""

from __future__ import annotations

import time
from pathlib import Path

LOG_PATH = Path.home() / ".claude" / "tool-engrams" / "watcher.log"


def _log(msg: str) -> None:
    """Append a timestamped line to the watcher log. Never raises."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass
