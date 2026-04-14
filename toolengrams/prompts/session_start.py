"""SessionStart formation guidance — injected at the start of every session."""

FORMATION_GUIDANCE = """\
[ToolEngrams: tool-bound memory]
You have ToolEngrams — a memory system for facts bound to specific tool calls. \
Memories surface automatically via PreToolUse when you call matching tools.

ONLY save things that are about how to use a specific command or file:
  Run: engram remember "<body>" --type <feedback|reference> \
--scope <global|project> [--name "<short name>"]

The body MUST include backticked commands (e.g. `git push`, `mycli -c`) or file \
paths. Triggers are extracted from these patterns — a body without them is rejected.

When to save (tool-bound facts only):
- User corrects how to use a command → type=feedback
- User confirms a non-obvious tool usage → type=feedback
- You learn how a specific CLI/tool/file should be used → type=reference

Do NOT save: user preferences, project deadlines, team info, or anything without \
a tool-call binding. Those belong in Claude's built-in memory system, not here.

To FORGET: engram forget "<name>"  |  To BROWSE: engram recall [query]\
"""
