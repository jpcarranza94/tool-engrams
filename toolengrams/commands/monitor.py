"""engram monitor — resource usage and observer activity."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .. import db


OBSERVER_LOG = Path.home() / ".claude" / "tool-engrams" / "observer.log"
DB_PATH = Path.home() / ".claude" / "tool-engrams" / "db.sqlite"


def main(argv: list[str] | None = None) -> int:
    conn = db.connect()
    try:
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

        # Running observer processes.
        try:
            ps = subprocess.run(
                ["pgrep", "-f", "engram observe"],
                capture_output=True, text=True,
            )
            observer_procs = len(ps.stdout.strip().splitlines()) if ps.stdout.strip() else 0
        except Exception:
            observer_procs = -1

        # Observer log stats (last 24h).
        observer_calls = 0
        observer_saves = 0
        observer_skips = 0
        if OBSERVER_LOG.exists():
            cutoff = time.strftime("%Y-%m-%d", time.localtime(day_ago))
            try:
                with open(OBSERVER_LOG) as f:
                    for line in f:
                        if line[:10] >= cutoff:
                            if "OBSERVE " in line:
                                observer_calls += 1
                            elif "RESULT" in line and "skip" not in line.lower():
                                observer_saves += 1
                            elif "SKIP" in line:
                                observer_skips += 1
            except Exception:
                pass

        # Orphan temp dirs.
        tmp = Path("/tmp") if Path("/tmp").exists() else Path(os.environ.get("TMPDIR", "/tmp"))
        orphan_dirs = len(list(tmp.glob("engram-observe-*")))

        result = {
            "db_size_kb": round(db_size / 1024, 1),
            "active_memories": active,
            "surfaces_24h": surfaces_today,
            "observer": {
                "running_processes": observer_procs,
                "calls_24h": observer_calls,
                "saves_24h": observer_saves,
                "skips_24h": observer_skips,
                "orphan_temp_dirs": orphan_dirs,
            },
            "pretool_latency_note": "~100ms per call (Python cold start)",
        }

        print(json.dumps(result, indent=2))
        return 0
    finally:
        conn.close()
