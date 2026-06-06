"""Flush hook — final watcher tick on session end / compaction.

Registered for both SessionEnd and PreCompact. The event-driven Stop trigger
handles the common case, but if a session ends (or its context is about to be
compacted away) there may be an unprocessed tail. A flush tick ignores the
coalesce debounce and the chat-gate so the last delta is never lost.

Output: {}.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ..utils import is_watcher_child
from ..watcher import derive_transcript_path, tick
from ._skip import is_internal_cwd


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        _emit({})
        return 0
    try:
        _maybe_flush(payload)
    except Exception:
        pass
    _emit({})
    return 0


def _maybe_flush(payload: dict[str, Any]) -> None:
    if is_watcher_child():
        return
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""
    if not session_id or not cwd or is_internal_cwd(cwd):
        return
    transcript_path = payload.get("transcript_path") or derive_transcript_path(session_id, cwd)
    tick.ensure_row(session_id, transcript_path, cwd)
    tick.trigger(session_id, transcript_path, cwd, reason="flush", flush=True)
    # Final pass: force closure on any still-pending surfaces (self-gated).
    tick.trigger_eval(session_id, transcript_path, cwd, reason="flush", flush=True)


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
