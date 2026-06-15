"""Shared transcript formatting limits and clipping helpers."""

from __future__ import annotations

# Cap the formatted delta sent to the model. Dormant sessions can accumulate
# huge transcripts (one observed at 345 KB = ~86K tokens on a single call).
# Long deltas both cost more and dilute the signal — the model starts
# narrating the whole conversation rather than spotting extractable patterns.
# Keep the tail since recent activity is most likely to contain extractable
# patterns (errors + corrections that happened this interval).
MAX_DELTA_CHARS = 40_000

# Per-line caps. The overall MAX_DELTA_CHARS budget isn't enough on its own: a
# single tool call can be enormous (a `gh pr create` / `git commit` heredoc
# carrying a multi-KB PR or commit body), and a single full error dump can run
# to thousands of lines. One such line eats the whole budget and pushes the
# model call past its timeout — exactly what stalled the watcher on busy
# multi-PR sessions. The signal the watcher needs lives in the head of a
# command (the binary + flags) and at both ends of an error (the command that
# failed + the cause), not in a PR body. Cap each accordingly.
MAX_BASH_CMD_CHARS = 800
MAX_RESULT_CHARS = 1_000


def _clip_head(text: str, limit: int) -> str:
    """Keep the first `limit` chars, flagging how much was dropped."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[+{len(text) - limit} chars truncated]"


def _clip_ends(text: str, limit: int) -> str:
    """Keep head + tail. Errors usually lead with the failing command and end
    with the actual cause, so preserve both ends and elide the middle."""
    if len(text) <= limit:
        return text
    # The elision marker itself is ~12 chars; for a tiny limit a head+tail split
    # would inflate the result past the input, so just hard-truncate the head.
    if limit < 24:
        return text[: max(limit, 0)]
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}…[+{len(text) - limit} chars]…{text[-tail:]}"


def _cap_delta(text: str) -> str:
    if len(text) <= MAX_DELTA_CHARS:
        return text
    tail = text[-MAX_DELTA_CHARS:]
    # Don't start the tail mid-line — trim to the first newline.
    nl = tail.find("\n")
    if 0 <= nl < 2000:
        tail = tail[nl + 1 :]
    dropped = len(text) - len(tail)
    return f"[…earlier activity truncated — {dropped} chars / {dropped // 80} lines dropped…]\n{tail}"
