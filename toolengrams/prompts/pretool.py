"""PreToolUse injection format — how memories are presented to Claude."""

PRIMARY_HEADER = "Relevant memories for this tool call:"
ASSOC_HEADER = "Related memories (surfaced nearby in this session):"


def format_injection(
    primary,
    associative=None,
    max_chars: int = 6000,
    max_body: int = 1200,
) -> str:
    """Format primary + associative memory candidates into additionalContext.

    Args:
        primary: List of Candidate objects that structurally match the tool call.
        associative: Optional list of Candidate objects linked to prior surfaces
                     in this session (Hebbian track). Rendered as a separate
                     labeled section below the primary list.
        max_chars: Total character budget for both sections combined.
        max_body: Per-memory body truncation length.

    Returns the formatted string, or "" if no memories fit.
    """
    associative = associative or []
    if not primary and not associative:
        return ""

    remaining = max_chars
    primary_blocks: list[str] = []
    for c in primary:
        block = _format_block(c, max_body)
        # +2 for the "\n\n" separator that joins blocks.
        if len(block) + 2 > remaining:
            break
        primary_blocks.append(block)
        remaining -= len(block) + 2

    assoc_blocks: list[str] = []
    for c in associative:
        block = _format_block(c, max_body)
        if len(block) + 2 > remaining:
            break
        assoc_blocks.append(block)
        remaining -= len(block) + 2

    sections: list[str] = []
    if primary_blocks:
        sections.append(PRIMARY_HEADER + "\n\n" + "\n\n".join(primary_blocks))
    if assoc_blocks:
        sections.append(ASSOC_HEADER + "\n\n" + "\n\n".join(assoc_blocks))

    return "\n\n".join(sections)


def _format_block(c, max_body: int) -> str:
    body = c.body.strip()
    if len(body) > max_body:
        body = body[: max_body - 1].rstrip() + "…"
    return f"[memory: {c.name}]\n{body}"
