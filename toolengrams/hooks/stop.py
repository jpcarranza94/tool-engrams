"""Stop hook — the primary event-driven watcher trigger.

Fires when the main agent finishes a turn. By then the whole arc (tool calls,
errors, the fix, the narration) is in the transcript, so it's the natural
"an episode just completed" moment to run a watcher pass. The tick itself
coalesces rapid turns and gates out pure-chat turns, so this hook just
fire-and-forgets a detached tick.

Output: {} (Stop hook takes no action on the session).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .. import pause
from ..target import get_target
from ..utils import is_watcher_child
from ..watcher import tick
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
        _maybe_tick(payload, get_target(target_name))
    except Exception:
        pass
    _emit({})
    return 0


def _maybe_tick(payload: dict[str, Any], target) -> None:
    # A watcher-launched `claude` must never trigger watcher ticks (recursion).
    if is_watcher_child():
        return
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""
    if not session_id or not cwd or is_internal_cwd(cwd):
        return
    transcript_path = target.transcript_path(payload)
    tick.ensure_row(session_id, transcript_path, cwd, target=target.NAME)
    tick.trigger(session_id, transcript_path, cwd, reason="stop", target=target.NAME)
    # Also judge surfaced memories from earlier in the session, if any are
    # pending (self-gated inside trigger_eval).
    tick.trigger_eval(session_id, transcript_path, cwd, reason="stop", target=target.NAME)


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
