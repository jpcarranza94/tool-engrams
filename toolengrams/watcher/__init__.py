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
    WATCHER_SCHEMA,
    _candidate_json_strings,
    _parse_response,
    _save_memory,
    _watcher_model,
)
from .lifecycle import (
    LOG_PATH,
    PYTHON_BIN,
    REPO_ROOT,
    SESSION_TIMEOUT,
    WATCHER_INTERVAL,
    _cleanup,
    _get_saved_cursor,
    _update_state,
    derive_transcript_path,
    main,
    spawn_watcher,
    watcher_main,
)
from .transcript_format import (
    MAX_DELTA_CHARS,
    _cap_delta,
    _format_delta,
    _is_session_alive,
    _read_lines_from,
)

__all__ = [
    # Public API
    "spawn_watcher",
    "derive_transcript_path",
    "main",
    "watcher_main",
    # Configuration constants
    "CLAUDE_BIN",
    "DEFAULT_WATCHER_MODEL",
    "LOG_PATH",
    "MAX_DELTA_CHARS",
    "PYTHON_BIN",
    "REPO_ROOT",
    "SESSION_TIMEOUT",
    "WATCHER_INTERVAL",
    "WATCHER_SCHEMA",
    # Internals re-exported for tests / introspection
    "_candidate_json_strings",
    "_cap_delta",
    "_cleanup",
    "_format_delta",
    "_get_saved_cursor",
    "_is_session_alive",
    "_parse_response",
    "_read_lines_from",
    "_save_memory",
    "_update_state",
    "_watcher_model",
]
