---
name: engram-remember
description: "Save a tool-bound memory to ToolEngrams. Use ONLY for facts about how to use specific commands, CLIs, or files — not for user/project facts (those go in Claude's built-in memory)."
---

# ToolEngrams: Remember

Save a tool-bound memory. The body MUST include backticked commands or file paths — triggers are extracted from those patterns. A body without tool patterns is rejected.

## When to Use

- User corrects how to use a command ("don't use pip, use uv pip install") → `--type feedback`
- User confirms a non-obvious tool usage ("yes, always --build with docker compose") → `--type feedback`
- You learn how a specific CLI, tool, or config file should be used → `--type reference`

## When NOT to Use

Do NOT save user preferences, project deadlines, team info, or anything that doesn't bind to a tool call. Those belong in Claude's built-in memory system (`Write` to `~/.claude/projects/.../memory/`), not ToolEngrams.

## Command

```bash
engram remember "<body>" \
  --type <feedback|reference> \
  --scope <global|project> \
  [--name "<short name>"] \
  [--extra-trigger "tool_head:Bash:git,push"] \
  [--extra-trigger "path_glob:**/*.py"]
```

## Writing Good Bodies

Include backticked commands so triggers extract automatically:

Good: "When running `mycli -c`, it's a read-only replica — SELECT only, no writes."
→ Extracts: (Bash, mycli) tool_head trigger

Good: "Config for hooks lives in `~/.claude/settings.json` — each entry is {matcher, hooks}."
→ Extracts: path_glob for settings.json

Bad: "The database is read-only." → REJECTED (no tool pattern to bind to).

## Dedup

If an existing memory already has overlapping triggers, the body is updated instead of creating a duplicate. This is automatic — just run the command normally.

## Defaults

- `--type reference`
- `--scope project` (scoped to current working directory)
- `--name` auto-synthesized from first line if not provided
