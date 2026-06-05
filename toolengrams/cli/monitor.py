"""engram monitor — resource usage and watcher activity."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import db


WATCHER_LOG = Path.home() / ".claude" / "tool-engrams" / "watcher.log"
DB_PATH = Path.home() / ".claude" / "tool-engrams" / "db.sqlite"


def main(argv: list[str] | None = None) -> int:
    with db.session() as conn:
        now = int(time.time())
        day_ago = now - 86400

        # DB size.
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

        # Surfaces today.
        surfaces_today = conn.execute(
            "SELECT COUNT(*) FROM session_surfaces WHERE surfaced_ts > ?",
            (day_ago,),
        ).fetchone()[0]

        # Active memories.
        active = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE archived_ts IS NULL"
        ).fetchone()[0]

        # Active watcher sessions.
        active_watchers = conn.execute(
            "SELECT COUNT(*) FROM watcher_state"
        ).fetchone()[0]

        # Watcher log stats (last 24h). The watcher is event-driven now: one
        # MODEL-* line per tick that called the model, SAVE per memory formed.
        watcher_model_calls = 0
        watcher_saves = 0
        watcher_errors = 0
        if WATCHER_LOG.exists():
            cutoff = time.strftime("%Y-%m-%d", time.localtime(day_ago))
            try:
                with open(WATCHER_LOG) as f:
                    for line in f:
                        if line[:10] >= cutoff:
                            # Independent counters: a MODEL-ERROR line counts as
                            # both a model call and an error.
                            if "MODEL-" in line:
                                watcher_model_calls += 1
                            if "SAVE " in line:
                                watcher_saves += 1
                            if "ERROR" in line:
                                watcher_errors += 1
            except Exception:
                pass

        result = {
            "db_size_kb": round(db_size / 1024, 1),
            "active_memories": active,
            "surfaces_24h": surfaces_today,
            "watcher": {
                "tracked_sessions": active_watchers,
                "model_calls_24h": watcher_model_calls,
                "saves_24h": watcher_saves,
                "errors_24h": watcher_errors,
            },
            "pretool_latency_note": "~100ms per call (Python cold start)",
        }

        print(json.dumps(result, indent=2))
        return 0
