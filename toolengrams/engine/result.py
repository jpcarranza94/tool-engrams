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
    # Spend, from the engine's own accounting. cost_usd stays None on engines
    # that report tokens but not dollars.
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
