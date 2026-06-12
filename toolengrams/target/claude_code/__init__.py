"""Claude Code target adapter.

The harness whose hook events created ToolEngrams' contracts, so most
functions here delegate to code that predates the seam: hint extraction
stays in `retrieval/extract.py` (it already speaks claude's tool
vocabulary), the transcript formatter lives in `transcript.py` (moved from
`watcher/transcript_format.py`), and the consolidation collector in
`collect.py` (moved from `consolidation/collect.py`).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ...retrieval.extract import ExtractedTriggerHint, extract_hints as _extract_hints
from ...utils import slugify_cwd
from .collect import collect_sessions as _collect_sessions
from .transcript import _format_delta

NAME = "claude-code"

# Claude Code emits a dedicated PostToolUseFailure event — that's what the
# minimum version buys; detect_failure below is only used by the PostToolUse
# recovery path.
min_version = "2.1.117"

# Tools whose pre/post-failure events trigger memory surfacing. New tools
# added to Claude Code that should bind memories go here, not in two places.
tool_whitelist: frozenset[str] = frozenset({
    "Bash", "Read", "Edit", "Write", "MultiEdit", "Grep", "Glob",
    "WebFetch", "NotebookEdit",
})

# Hook event -> the command marker install.sh wires for it (the --target
# suffix is matched by doctor's startswith check). doctor's wiring check and
# the uninstaller's settings surgery both key on these, so the three stay in
# lockstep.
_HOOK_MARKERS = {
    "SessionStart": "engram session-start",
    "UserPromptSubmit": "engram user-prompt",
    "PreToolUse": "engram pretool",
    "PostToolUse": "engram post-tool",
    "PostToolUseFailure": "engram post-tool-failure",
    "Stop": "engram stop",
    "SessionEnd": "engram flush",
    "PreCompact": "engram flush",
}


def extract_hints(tool_name: str, tool_input: dict) -> ExtractedTriggerHint:
    return _extract_hints(tool_name, tool_input)


def detect_failure(payload: dict) -> bool:
    """Did this PostToolUse payload describe a failed call?

    Claude Code provides is_error directly in some cases. For Bash, we also
    check for non-zero exit codes or stderr markers in the response.
    """
    if payload.get("is_error"):
        return True
    response = payload.get("tool_response") or ""
    if isinstance(response, str):
        # Claude Code wraps Bash errors in an <error> tag or prefixes with "Exit code"
        if response.startswith("<error>") or "Exit code" in response[:50]:
            return True
    return False


def transcript_path(payload: dict) -> str:
    """Payload-first; falls back to deriving from Claude Code's projects
    layout (`~/.claude/projects/<slug>/<session_id>.jsonl`)."""
    given = payload.get("transcript_path") or ""
    if given:
        return given
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""
    if not session_id or not cwd:
        return ""
    return derive_transcript_path(session_id, cwd)


def derive_transcript_path(session_id: str, cwd: str) -> str:
    """Derive the JSONL transcript path from session_id and cwd."""
    slug = slugify_cwd(cwd)
    return str(Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl")


def format_delta(lines: list[str]) -> str:
    return _format_delta(lines)


def collect_sessions(target_date: date, projects_dir: Path | None = None):
    return _collect_sessions(target_date, projects_dir=projects_dir)


def hook_markers() -> dict[str, str]:
    return dict(_HOOK_MARKERS)
