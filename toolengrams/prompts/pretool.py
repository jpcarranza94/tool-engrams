"""PreToolUse injection format — how memories are presented to Claude."""

INJECTION_HEADER = "Relevant memories for this tool call:"


def format_injection(selected_candidates, max_chars: int = 6000, max_body: int = 1200) -> str:
    """Format selected memory candidates into the additionalContext string."""
    parts: list[str] = []
    remaining = max_chars
    for c in selected_candidates:
        body = c.body.strip()
        if len(body) > max_body:
            body = body[:max_body - 1].rstrip() + "…"
        block = f"[memory: {c.name}]\n{body}"
        if len(block) + 2 > remaining:
            break
        parts.append(block)
        remaining -= len(block) + 2
    return INJECTION_HEADER + "\n\n" + "\n\n".join(parts) if parts else ""
