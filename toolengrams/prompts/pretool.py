"""PreToolUse injection format — how memories are presented to Claude."""

PRIMARY_HEADER = "Relevant memories for this tool call:"


def format_injection(
    primary,
    max_chars: int = 6000,
    max_body: int = 1200,
) -> str:
    """Format matched memory candidates into additionalContext text.

    Args:
        primary: List of Candidate objects that structurally match the tool call.
        max_chars: Total character budget.
        max_body: Per-memory body truncation length.

    Returns the formatted string, or "" if nothing fits.
    """
    if not primary:
        return ""

    remaining = max_chars
    blocks: list[str] = []
    for c in primary:
        block = _format_block(c, max_body)
        if len(block) + 2 > remaining:
            break
        blocks.append(block)
        remaining -= len(block) + 2

    if not blocks:
        return ""
    return PRIMARY_HEADER + "\n\n" + "\n\n".join(blocks)


def _format_block(c, max_body: int) -> str:
    body = c.body.strip()
    if len(body) > max_body:
        body = body[: max_body - 1].rstrip() + "…"
    return f"[memory: {c.name}]\n{body}"
