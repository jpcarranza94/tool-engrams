"""PreToolUse injection format — how memories are presented to Claude.

Memories are sorted by specificity (longest matching trigger first) then
by relevance score. The injection labels each memory with its kind
(block/hint) so Claude understands the weight: blocks are corrections
that prevented the call; hints are contextual guidance.
"""

HEADER = "Relevant memories for this tool call (ordered by relevance):"


def format_injection(
    candidates,
    max_chars: int = 6000,
    max_body: int = 1200,
) -> str:
    """Format matched memory candidates into additionalContext text.

    Candidates are pre-sorted by the hook (specificity DESC, score DESC).
    Each memory is labeled with its kind so Claude can prioritize:
      [block: name] — this is a correction, the call was denied for this reason
      [hint: name]  — contextual guidance, take into account

    Returns the formatted string, or "" if nothing fits.
    """
    if not candidates:
        return ""

    remaining = max_chars
    blocks: list[str] = []
    for c in candidates:
        block = _format_block(c, max_body)
        if len(block) + 2 > remaining:
            break
        blocks.append(block)
        remaining -= len(block) + 2

    if not blocks:
        return ""
    return HEADER + "\n\n" + "\n\n".join(blocks)


def _format_block(c, max_body: int) -> str:
    body = c.body.strip()
    if len(body) > max_body:
        body = body[: max_body - 1].rstrip() + "…"
    return f"[{c.kind}: {c.name}]\n{body}"
