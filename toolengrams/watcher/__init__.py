"""Persistent parallel watcher: background LLM session for memory formation.

Public surface:

  spawn_watcher(session_id, transcript_path, cwd) — fork a detached watcher
  derive_transcript_path(session_id, cwd) — JSONL location helper
  main(argv)                              — CLI entry for `engram watcher ...`

The model is configurable via `ENGRAM_WATCHER_MODEL` (default: `opus`).
Opus produces sharper extractions on noisy transcripts; switch to `haiku`
for ~20× cost reduction at the cost of more parse errors.

Module layout:
  - transcript_format.py — pure JSONL → readable-conversation
  - agent.py             — claude -p invocation + JSON response parsing
  - lifecycle.py         — DB cursor + cron loop + spawn / cleanup
"""

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
from .lifecycle import (
    LOG_PATH,
    MAX_FORM_RETRIES,
    PYTHON_BIN,
    REPO_ROOT,
    SESSION_TIMEOUT,
    WATCHER_INTERVAL,
    _cleanup,
    _get_saved_cursor,
    _retry_decision,
    _update_state,
    derive_transcript_path,
    main,
    spawn_watcher,
    watcher_main,
)
from .transcript_format import (
    MAX_BASH_CMD_CHARS,
    MAX_DELTA_CHARS,
    MAX_RESULT_CHARS,
    _cap_delta,
    _clip_ends,
    _clip_head,
    _format_delta,
    _is_session_alive,
    _read_lines_from,
)
from . import tick  # noqa: E402  (event-driven tick engine; imported last)

__all__ = [
    # Public API
    "spawn_watcher",
    "derive_transcript_path",
    "main",
    "watcher_main",
    "tick",
    # Configuration constants
    "CLAUDE_BIN",
    "DEFAULT_WATCHER_MODEL",
    "DEFAULT_WATCHER_TIMEOUT",
    "LOG_PATH",
    "MAX_BASH_CMD_CHARS",
    "MAX_DELTA_CHARS",
    "MAX_FORM_RETRIES",
    "MAX_RESULT_CHARS",
    "PYTHON_BIN",
    "REPO_ROOT",
    "SESSION_TIMEOUT",
    "WATCHER_INTERVAL",
    "WATCHER_SCHEMA",
    # Internals re-exported for tests / introspection
    "_candidate_json_strings",
    "_cap_delta",
    "_cleanup",
    "_clip_ends",
    "_clip_head",
    "_format_delta",
    "_get_saved_cursor",
    "_is_session_alive",
    "_parse_response",
    "_read_lines_from",
    "_save_memory",
    "_update_state",
    "_watcher_model",
    "_watcher_timeout",
]
