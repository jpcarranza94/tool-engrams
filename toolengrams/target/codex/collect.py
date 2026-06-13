"""Transcript collection for Codex rollout sessions."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ...utils import slugify_cwd
from ..interface import SessionFile

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def collect_sessions(
    target_date: date,
    sessions_dir: Path | None = None,
) -> list[SessionFile]:
    base = sessions_dir or CODEX_SESSIONS_DIR
    day_dir = base / f"{target_date:%Y}" / f"{target_date:%m}" / f"{target_date:%d}"
    if not day_dir.is_dir():
        return []

    results: list[SessionFile] = []
    for rollout in day_dir.glob("rollout-*.jsonl"):
        stat = rollout.stat()
        session_id, project_slug = _session_meta(rollout)
        results.append(SessionFile(
            path=rollout,
            session_id=session_id or rollout.stem,
            project_slug=project_slug,
            modified_ts=stat.st_mtime,
            size_bytes=stat.st_size,
        ))
    results.sort(key=lambda s: s.modified_ts)
    return results


def _session_meta(path: Path) -> tuple[str, str]:
    try:
        with path.open() as f:
            for line in f:
                obj = json.loads(line)
                if obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload") or {}
                session_id = payload.get("id") or ""
                cwd = payload.get("cwd") or ""
                return str(session_id), slugify_cwd(str(cwd)) if cwd else ""
    except (OSError, json.JSONDecodeError):
        pass
    return "", ""
