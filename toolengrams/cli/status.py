"""engram status — memory health dashboard."""

from __future__ import annotations

import json

from .. import db, memory_store
from ..consolidation import runs
from ..consolidation.schedule import is_installed as schedule_is_installed


def main(argv: list[str] | None = None) -> int:
    with db.session() as conn:
        health = memory_store.health_stats(conn)
        last_run = runs.last_run(conn)

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
