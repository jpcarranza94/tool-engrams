"""Event-driven watcher: background LLM memory formation, fired by hooks.

Hooks fire a detached `engram watcher-tick` per meaningful event; each tick
reads the transcript delta since its cursor, gates out pure-chat turns, calls
`claude -p`, and saves. Model via `$ENGRAM_WATCHER_MODEL` (default: opus).

Module layout:
  - transcript_format.py — pure JSONL → readable-conversation
  - agent.py             — claude -p invocation + JSON response parsing
  - state.py             — watcher_state persistence (cursor / armed / streak)
  - log.py               — watcher log sink
  - tick.py              — event-driven tick engine + coalesce + idle sweep
"""

from . import tick
from .agent import (
    CLAUDE_BIN,
    DEFAULT_WATCHER_MODEL,
    DEFAULT_WATCHER_TIMEOUT,
    WATCHER_SCHEMA,
    _candidate_json_strings,
    _parse_response,
    _save_memory,
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
    "tick",
    "derive_transcript_path",
    # Configuration constants
    "CLAUDE_BIN",
    "DEFAULT_WATCHER_MODEL",
    "DEFAULT_WATCHER_TIMEOUT",
    "LOG_PATH",
    "MAX_BASH_CMD_CHARS",
    "MAX_DELTA_CHARS",
    "MAX_FORM_RETRIES",
    "MAX_RESULT_CHARS",
    "WATCHER_SCHEMA",
    # Internals re-exported for tests / introspection
    "_candidate_json_strings",
    "_cap_delta",
    "_clip_ends",
    "_clip_head",
    "_format_delta",
    "_parse_response",
    "_read_lines_from",
    "_retry_decision",
    "_save_memory",
    "_watcher_model",
    "_watcher_timeout",
]
