"""engram status — memory health dashboard."""

from __future__ import annotations

import json
import sys

from .. import db
from ..consolidation.schedule import is_installed as schedule_is_installed


def main(argv: list[str] | None = None) -> int:
    conn = db.connect()
    try:
        # Memory counts.
        mem_stats = conn.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN archived_ts IS NULL THEN 1 ELSE 0 END) AS active, "
            "  SUM(CASE WHEN archived_ts IS NOT NULL THEN 1 ELSE 0 END) AS archived, "
            "  SUM(CASE WHEN archived_ts IS NULL THEN surface_count ELSE 0 END) AS total_surfaces, "
            "  SUM(CASE WHEN archived_ts IS NULL THEN useful_count ELSE 0 END) AS total_useful "
            "FROM memories"
        ).fetchone()

        # Trigger counts.
        trigger_stats = conn.execute(
            "SELECT t.kind AS kind, COUNT(*) AS count FROM triggers t "
            "JOIN memories m ON t.memory_id = m.id WHERE m.archived_ts IS NULL "
            "GROUP BY t.kind"
        ).fetchall()

        # Last consolidation run.
        last_run = conn.execute(
            "SELECT run_date, sessions_scanned, memories_archived, "
            "memories_discovered, memories_strengthened, memories_weakened "
            "FROM consolidation_runs ORDER BY started_ts DESC LIMIT 1"
        ).fetchone()

        # Schedule status.
        schedule_installed = schedule_is_installed()

        result = {
            "memories": {
                "active": mem_stats["active"] or 0,
                "archived": mem_stats["archived"] or 0,
                "total_surfaces": mem_stats["total_surfaces"] or 0,
                "total_useful": mem_stats["total_useful"] or 0,
            },
            "triggers": {r["kind"]: r["count"] for r in trigger_stats},
            "last_consolidation": dict(last_run) if last_run else None,
            "schedule_installed": schedule_installed,
        }

        print(json.dumps(result, indent=2))
        return 0
    finally:
        conn.close()
