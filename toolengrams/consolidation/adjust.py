"""Mechanical score adjustment — no LLM required.

Phase 2 consolidation runs these checks:
  1. Auto-archive dead memories (high surface, low usefulness)
  2. Flag stale memories (not surfaced in 2× half-life)
  3. Clean up old session_surfaces rows (TTL)
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from ..rank import HALF_LIFE_DAYS

# Auto-archive: surfaced ≥ this many times with usefulness below threshold.
ARCHIVE_MIN_SURFACES = 8
ARCHIVE_USEFULNESS_THRESHOLD = 0.2

# Session surfaces TTL: rows older than this are deleted.
SURFACES_TTL_DAYS = 30


@dataclass(slots=True)
class AdjustmentReport:
    archived_ids: list[int] = field(default_factory=list)
    archived_names: list[str] = field(default_factory=list)
    stale_ids: list[int] = field(default_factory=list)
    stale_names: list[str] = field(default_factory=list)
    surfaces_cleaned: int = 0


def run_mechanical_adjustments(conn: sqlite3.Connection) -> AdjustmentReport:
    """Run all mechanical consolidation checks. Returns a report."""
    report = AdjustmentReport()
    now_ts = int(time.time())

    _auto_archive_dead(conn, now_ts, report)
    _flag_stale(conn, now_ts, report)
    _cleanup_surfaces(conn, now_ts, report)

    return report


def _auto_archive_dead(
    conn: sqlite3.Connection,
    now_ts: int,
    report: AdjustmentReport,
) -> None:
    """Archive memories with high surface_count but usefulness < threshold."""
    rows = conn.execute(
        """
        SELECT id, name, surface_count, useful_count
        FROM memories
        WHERE archived_ts IS NULL
          AND surface_count >= ?
        """,
        (ARCHIVE_MIN_SURFACES,),
    ).fetchall()

    for row in rows:
        usefulness = (row["useful_count"] + 1.0) / (row["surface_count"] + 2.0)
        if usefulness < ARCHIVE_USEFULNESS_THRESHOLD:
            conn.execute(
                "UPDATE memories SET archived_ts = ? WHERE id = ?",
                (now_ts, row["id"]),
            )
            report.archived_ids.append(row["id"])
            report.archived_names.append(row["name"])


def _flag_stale(
    conn: sqlite3.Connection,
    now_ts: int,
    report: AdjustmentReport,
) -> None:
    """Identify memories not surfaced in 2× their type's half-life."""
    rows = conn.execute(
        """
        SELECT id, name, type, last_surfaced_ts, created_ts
        FROM memories
        WHERE archived_ts IS NULL
          AND last_surfaced_ts > 0
        """,
    ).fetchall()

    for row in rows:
        half_life = HALF_LIFE_DAYS.get(row["type"], 60.0)
        stale_threshold_seconds = half_life * 2 * 86400
        last_active = row["last_surfaced_ts"]
        if (now_ts - last_active) > stale_threshold_seconds:
            report.stale_ids.append(row["id"])
            report.stale_names.append(row["name"])


def _cleanup_surfaces(
    conn: sqlite3.Connection,
    now_ts: int,
    report: AdjustmentReport,
) -> None:
    """Delete session_surfaces rows older than TTL."""
    cutoff = now_ts - (SURFACES_TTL_DAYS * 86400)
    cursor = conn.execute(
        "DELETE FROM session_surfaces WHERE surfaced_ts < ?",
        (cutoff,),
    )
    report.surfaces_cleaned = cursor.rowcount
