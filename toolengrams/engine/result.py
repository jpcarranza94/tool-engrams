"""EngineResult — the outcome of one headless engine invocation.

Process failures (timeout, spawn error, missing binary, non-zero exit) come
back as flags here, never raised — callers decide whether that's fatal.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class EngineResult:
    ok: bool
    engine: str = ""
    stdout: str = ""
    returncode: int = 0
    timed_out: bool = False
    error: str | None = None
    # The final response: free text (consolidation's report) or, when the
    # request carried a schema, the constrained JSON — same precedence the
    # claude adapter always had (structured beats free text).
    text: str = ""
    # The engine's own id for the conversation this call ran in, when it keeps
    # one (claude-code's result envelope carries a session_id). Lets a caller
    # continue the SAME session via EngineRequest.resume_session_id — the basis
    # for the consolidation report-correction retry. Stays None on engines that
    # don't persist a session (codex runs `--ephemeral`).
    session_id: str | None = None
    # Spend, from the engine's own accounting. cost_usd stays None on engines
    # that report tokens but not dollars.
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
