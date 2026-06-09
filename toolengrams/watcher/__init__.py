"""Event-driven watcher: background LLM memory formation + evaluation.

Hooks fire a detached `engram watcher-tick` per meaningful event; each tick reads
the transcript delta since its (session, role) cursor and runs a permissioned
`claude -p` session that does its job by calling the engram CLI — `engram
remember` (formation) or `engram judge` (evaluation). No JSON schema, no parsing.
Model via `$ENGRAM_WATCHER_MODEL` (default opus).

Module layout:
  - transcript_format.py — pure JSONL → readable-conversation
  - agent.py             — permissioned claude -p session runner (per role)
  - state.py             — watcher_state persistence, keyed (session, role)
  - log.py               — watcher log sink
  - tick.py              — event-driven tick engine + coalesce + idle sweep
  - cleanup.py           — once-daily reaper of cold watcher residue
"""

from . import cleanup, tick
from .agent import (
    CLAUDE_BIN,
    DEFAULT_WATCHER_MODEL,
    DEFAULT_WATCHER_TIMEOUT,
    ROLE_ALLOWLIST,
    SessionResult,
    run_watcher_session,
    _extract_session_id,
    _watcher_model,
    _watcher_timeout,
)
from .log import LOG_PATH
from .state import derive_transcript_path
from .tick import MAX_FORM_RETRIES, _retry_decision
from .transcript_format import (
    MAX_BASH_CMD_CHARS,
    MAX_DELTA_CHARS,
    MAX_RESULT_CHARS,
    _cap_delta,
    _clip_ends,
    _clip_head,
    _format_delta,
    _read_lines_from,
)

__all__ = [
    # Public API
    "cleanup",
    "tick",
    "derive_transcript_path",
    "run_watcher_session",
    # Configuration constants
    "CLAUDE_BIN",
    "DEFAULT_WATCHER_MODEL",
    "DEFAULT_WATCHER_TIMEOUT",
    "LOG_PATH",
    "MAX_BASH_CMD_CHARS",
    "MAX_DELTA_CHARS",
    "MAX_FORM_RETRIES",
    "MAX_RESULT_CHARS",
    "ROLE_ALLOWLIST",
    "SessionResult",
    # Internals re-exported for tests / introspection
    "_cap_delta",
    "_clip_ends",
    "_clip_head",
    "_extract_session_id",
    "_format_delta",
    "_read_lines_from",
    "_retry_decision",
    "_watcher_model",
    "_watcher_timeout",
]
