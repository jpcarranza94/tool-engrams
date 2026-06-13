"""Codex target adapter."""

from __future__ import annotations

import json
from pathlib import Path

from ...models import ExtractedTriggerHint
from ...retrieval.extract import extract_hints as _extract_hints
from . import collect
from .collect import collect_sessions as _collect_sessions
from .patch_parse import paths_from_patch
from .transcript import format_delta as _format_delta

NAME = "codex"
min_version = "0.137.0"
has_failure_event = False

tool_whitelist: frozenset[str] = frozenset({"Bash", "apply_patch"})

_HOOK_MARKERS = {
    "SessionStart": "engram session-start",
    "UserPromptSubmit": "engram user-prompt",
    "PreToolUse": "engram pretool",
    "PostToolUse": "engram post-tool",
    "Stop": "engram stop",
    "PreCompact": "engram flush",
}


def extract_hints(tool_name: str, tool_input: dict) -> ExtractedTriggerHint:
    if tool_name == "Bash":
        return _extract_hints(tool_name, tool_input)
    if tool_name == "apply_patch":
        return ExtractedTriggerHint(
            tool_name=tool_name,
            paths=paths_from_patch(_patch_text(tool_input)),
        )
    return ExtractedTriggerHint(tool_name=tool_name)


def _patch_text(tool_input) -> str:
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return ""
    for key in ("patch", "input", "command", "cmd"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def detect_failure(payload: dict) -> bool:
    response = payload.get("tool_response")
    if isinstance(response, dict):
        if response.get("ok") is False or response.get("success") is False:
            return True
        for key in ("exit_code", "exitCode", "code"):
            value = response.get(key)
            if isinstance(value, int) and value != 0:
                return True
        return False
    if isinstance(response, str):
        head = response[:200]
        return (
            "Process exited with code 0" not in head
            and "Exit code: 0" not in head
            and (
                "Process exited with code " in head
                or "Exit code: " in head
                or "exited with code " in head
            )
        )
    return False


def transcript_path(payload: dict) -> str:
    return payload.get("transcript_path") or ""


def format_delta(lines: list[str]) -> str:
    return _format_delta(lines)


def collect_sessions(target_date, projects_dir: Path | None = None):
    return _collect_sessions(target_date, sessions_dir=projects_dir)


def hook_markers() -> dict[str, str]:
    return dict(_HOOK_MARKERS)


def is_wired() -> bool:
    path = Path.home() / ".codex" / "hooks.json"
    try:
        hooks = json.loads(path.read_text()).get("hooks", {})
    except (OSError, json.JSONDecodeError):
        return False
    for event, marker in _HOOK_MARKERS.items():
        if not any(
            h.get("command", "") == marker or h.get("command", "").startswith(marker + " ")
            for entry in hooks.get(event, [])
            for h in entry.get("hooks", [])
        ):
            return False
    return True
