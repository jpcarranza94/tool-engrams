"""UserPromptSubmit hook — fire a watcher tick on a likely user correction.

Most prompts don't need a tick: the Stop hook after Claude acts handles normal
formation. But when the user's message looks like a CORRECTION of the prior
turn ("no, use X", "that's wrong", "actually ..."), that's a high-value memory
signal — the correction is the lesson. We fire a tick now, while it's fresh, so
the watcher pairs the corrected behavior with what the user said.

Also (re)registers the session in watcher_state so its cursor is tracked.

Output: {} (no injection).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ..utils import is_watcher_child
from ..watcher import derive_transcript_path, tick
from ._skip import is_internal_cwd

# Lowercase cues that suggest the user is correcting the prior turn. Kept fairly
# specific: a false positive only costs one coalesced tick (and the tick's own
# chat-gate skips it cheaply if there's no tool activity), while a miss is
# caught by the next Stop. So bias toward precision over recall here.
_CORRECTION_CUES = (
    "no,", "nope", "actually", "instead", "wrong", "incorrect", "that's not",
    "thats not", "don't", "revert", "undo", "should be", "rather than",
    "mistake", "not what", "that's not right", "no need",
)
_MAX_CORRECTION_LEN = 280


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        _emit({})
        return 0
    try:
        _maybe_tick_on_correction(payload)
    except Exception:
        pass
    _emit({})
    return 0


def _looks_like_correction(prompt: str) -> bool:
    text = (prompt or "").strip().lower()
    if not text or len(text) > _MAX_CORRECTION_LEN:
        return False
    return any(cue in text for cue in _CORRECTION_CUES)


def _maybe_tick_on_correction(payload: dict[str, Any]) -> None:
    # A watcher-launched `claude` must never trigger watcher ticks (recursion).
    if is_watcher_child():
        return
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""
    if not session_id or not cwd or is_internal_cwd(cwd):
        return
    transcript_path = payload.get("transcript_path") or derive_transcript_path(session_id, cwd)
    tick.ensure_row(session_id, transcript_path, cwd)
    if _looks_like_correction(payload.get("prompt", "")):
        tick.trigger(session_id, transcript_path, cwd, reason="user-correction")


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
