"""_get_memory_summary orders audit-first: never-verified, then oldest-verified."""

from __future__ import annotations

import os
import time
from pathlib import Path

from toolengrams.consolidation.agent import _get_memory_summary


def _seed(conn, name: str, verified_ts: int | None) -> None:
    now_ts = int(time.time())
    conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, project_slug, "
        "                      created_ts, last_verified_ts) "
        "VALUES (?, '', 'b', 'hint', 'global', NULL, ?, ?)",
        (name, now_ts, verified_ts),
    )


def test_never_verified_appears_before_old_verified(temp_db):
    _seed(temp_db, "old-verified", int(time.time()) - 30 * 86400)
    _seed(temp_db, "never-verified", None)
    _seed(temp_db, "recent-verified", int(time.time()) - 5 * 86400)

    summary = _get_memory_summary(Path(os.environ["ENGRAM_DB"]))

    idx_never = summary.index("never-verified")
    idx_old = summary.index("old-verified")
    idx_recent = summary.index("recent-verified")
    assert idx_never < idx_old < idx_recent
