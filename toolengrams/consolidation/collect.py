"""Transcript collection: find today's JSONL session files across all projects."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


@dataclass(slots=True)
class SessionFile:
    path: Path
    session_id: str
    project_slug: str
    modified_ts: float
    size_bytes: int


def collect_sessions(
    target_date: date,
    projects_dir: Path | None = None,
) -> list[SessionFile]:
    """Find all JSONL session files modified on the target date."""
    base = projects_dir or CLAUDE_PROJECTS_DIR
    if not base.is_dir():
        return []

    results: list[SessionFile] = []
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        project_slug = project_dir.name

        for jsonl in project_dir.glob("*.jsonl"):
            mtime = jsonl.stat().st_mtime
            mdate = datetime.fromtimestamp(mtime).date()  # local time
            if mdate != target_date:
                continue

            session_id = jsonl.stem
            results.append(SessionFile(
                path=jsonl,
                session_id=session_id,
                project_slug=project_slug,
                modified_ts=mtime,
                size_bytes=jsonl.stat().st_size,
            ))

    results.sort(key=lambda s: s.modified_ts)
    return results
