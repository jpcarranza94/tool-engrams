"""Transcript collection: find today's JSONL session files across all projects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# A project slug is `cwd.replace("/", "-")` (see utils.slugify_cwd).
# Our internal temp dirs use `tempfile.mkdtemp(prefix="engram-{role}-")`,
# which produces a basename like `engram-consolidate-Z3K9q1` (alphanumeric
# random suffix, no further dashes). After slugification, this trails the
# slug. Match the suffix anchored at end-of-string, with the random part
# constrained to non-dash chars so we don't accidentally swallow a deeper
# project path that happens to contain "engram-consolidate" in the middle.
_INTERNAL_PROJECT_RE = re.compile(
    r"engram-(?:observe|consolidate|experiment)-[A-Za-z0-9_]+$"
)


def _is_internal_project(project_slug: str) -> bool:
    """True if the slug looks like one of our own temp-dir sessions."""
    return bool(_INTERNAL_PROJECT_RE.search(project_slug))


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

        # Skip ToolEngrams' own sessions (observer, consolidation, experiments).
        if _is_internal_project(project_slug):
            continue

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
