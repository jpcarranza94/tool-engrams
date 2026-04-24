"""Shared subprocess utilities for agent spawning."""

from __future__ import annotations

import json
from pathlib import Path


def parse_claude_json_output(stdout: str) -> str:
    """Extract the result text from claude -p --output-format json output.

    When --json-schema is used, the constrained JSON response is in the
    `structured_output` field (already a dict). The `result` field contains
    free-form text summary which is NOT the structured data.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                # Prefer structured_output (from --json-schema constrained decoding).
                so = payload.get("structured_output")
                if so is not None:
                    return json.dumps(so) if isinstance(so, dict) else str(so)
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
