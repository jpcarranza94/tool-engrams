"""Codex target adapter."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ...harness_names import CODEX
from ...models import ExtractedTriggerHint
from ...retrieval.extract import extract_hints as _extract_hints
from . import collect
from .collect import collect_sessions as _collect_sessions
from .patch_parse import paths_from_patch
from .transcript import format_delta as _format_delta

NAME = CODEX
cli_binary = "codex"
min_version = "0.137.0"
has_failure_event = False

tool_whitelist: frozenset[str] = frozenset({"Bash", "apply_patch"})

_HOOK_MARKERS = {
    "SessionStart": "engram session-start --target codex",
    "UserPromptSubmit": "engram user-prompt --target codex",
    "PreToolUse": "engram pretool --target codex",
    "PostToolUse": "engram post-tool --target codex",
    "Stop": "engram stop --target codex",
    "PreCompact": "engram flush --target codex",
}

_FAILURE_PREFIXES = (
    "fatal:",
    "traceback (most recent call last):",
    "apply_patch verification failed",
    "verification failed:",
    "failed to read file",
    "failed to apply patch",
)
_DIAGNOSTIC_RE = re.compile(
    r"^[\w./-]+: .*"
    r"(command not found|no such file or directory|permission denied|"
    r"operation not permitted|not a git repository|os error \d+)"
)


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
        # Codex Bash PostToolUse currently gives hooks only aggregated output,
        # not status. Nonzero exits with no clear diagnostic are undetectable.
        return _string_response_failed(response)
    return False


def _string_response_failed(response: str) -> bool:
    for line in response.splitlines():
        text = line.strip().lower()
        if not text:
            continue
        if text.startswith(_FAILURE_PREFIXES):
            return True
        if _DIAGNOSTIC_RE.search(text):
            return True
    return False


def transcript_path(payload: dict) -> str:
    return payload.get("transcript_path") or ""


def format_delta(lines: list[str]) -> str:
    return _format_delta(lines)


def collect_sessions(target_date, projects_dir: Path | None = None):
    return _collect_sessions(target_date, sessions_dir=projects_dir)


def hook_markers() -> dict[str, str]:
    return dict(_HOOK_MARKERS)


def _load_hooks() -> dict | None:
    path = Path.home() / ".codex" / "hooks.json"
    try:
        return json.loads(path.read_text()).get("hooks", {})
    except (OSError, json.JSONDecodeError):
        return None


def _hooks_feature_enabled() -> bool:
    path = Path.home() / ".codex" / "config.toml"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return False
    in_features = False
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = stripped == "[features]"
            continue
        if in_features and stripped.startswith("hooks"):
            key, sep, value = stripped.partition("=")
            if sep and key.strip() == "hooks":
                return value.strip().lower() == "true"
    return False


def hook_status() -> dict[str, object]:
    hooks = _load_hooks()
    markers = hook_markers()
    if hooks is None:
        return {"seen": False, "missing": list(markers), "total": len(markers)}
    matched: list[str] = []
    missing: list[str] = []
    for event, marker in markers.items():
        if _event_has_marker(hooks, event, marker):
            matched.append(event)
        else:
            missing.append(event)
    if matched and not _hooks_feature_enabled():
        missing.append("features.hooks")
    return {"seen": bool(matched), "missing": missing, "total": len(markers)}


def installed_version() -> str | None:
    try:
        out = subprocess.run([cli_binary, "--version"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\d+\.\d+\.\d+", out or "")
    return match.group(0) if match else None


def is_wired() -> bool:
    status = hook_status()
    return bool(status["seen"]) and not status["missing"]


def _event_has_marker(hooks: dict, event: str, marker: str) -> bool:
    return any(
        h.get("command", "") == marker or h.get("command", "").startswith(marker + " ")
        for entry in hooks.get(event, [])
        for h in entry.get("hooks", [])
    )
