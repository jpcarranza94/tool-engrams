"""engram status — memory health dashboard."""

from __future__ import annotations

import json
import sys

from .. import db, memory_store
from ..consolidation.schedule import is_installed as schedule_is_installed


def main(argv: list[str] | None = None) -> int:
    with db.session() as conn:
        health = memory_store.health_stats(conn)

        # Last consolidation run.
        last_run = conn.execute(
            "SELECT run_date, sessions_scanned, memories_archived, "
            "memories_discovered, memories_strengthened, memories_weakened "
            "FROM consolidation_runs ORDER BY started_ts DESC LIMIT 1"
        ).fetchone()

        result = {
            "memories": {
                "active": health["active"],
                "archived": health["archived"],
                "total_surfaces": health["total_surfaces"],
                "total_useful": health["total_useful"],
            },
            "triggers": health["triggers_by_kind"],
            "last_consolidation": dict(last_run) if last_run else None,
            "schedule_installed": schedule_is_installed(),
        }

        print(json.dumps(result, indent=2))
        return 0
