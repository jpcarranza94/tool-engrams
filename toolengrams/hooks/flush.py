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

from .. import pause
from ..utils import is_watcher_child
from ..watcher import tick
from ..target import get_target
from ._skip import is_internal_cwd


def main(target_name: str = "claude-code") -> int:
    if pause.is_disabled():
        _emit({})
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        _emit({})
        return 0
    try:
        _maybe_flush(payload, get_target(target_name))
    except Exception:
        pass
    _emit({})
    return 0


def _maybe_flush(payload: dict[str, Any], target) -> None:
    if is_watcher_child():
        return
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""
    if not session_id or not cwd or is_internal_cwd(cwd):
        return
    transcript_path = target.transcript_path(payload)
    tick.ensure_row(session_id, transcript_path, cwd, target=target.NAME)
    tick.trigger(session_id, transcript_path, cwd, reason="flush", flush=True,
                 target=target.NAME)
    # Final pass: force closure on any still-pending surfaces (self-gated).
    tick.trigger_eval(session_id, transcript_path, cwd, reason="flush", flush=True,
                      target=target.NAME)


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
