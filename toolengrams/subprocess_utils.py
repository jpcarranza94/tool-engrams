"""Shared subprocess utilities for agent spawning."""

from __future__ import annotations

import json
from pathlib import Path


def parse_claude_json_output(stdout: str) -> str:
    """Extract the result text from claude -p --output-format json output."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                return payload.get("result", "")
            except json.JSONDecodeError:
                continue
    return ""


def write_agent_settings(work_dir: Path, permissions: list[str]) -> None:
    """Write .claude/settings.local.json granting specified permissions."""
    settings_dir = work_dir / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings = {"permissions": {"allow": permissions}}
    (settings_dir / "settings.local.json").write_text(json.dumps(settings, indent=2))
