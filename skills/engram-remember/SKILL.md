---
name: engram-remember
description: "Save a memory to ToolEngrams with auto-extracted triggers. Use when you learn something worth remembering: user corrections, confirmed approaches, role/project facts, references."
---

# ToolEngrams: Remember

Save a memory to the tool-bound memory store. Triggers are auto-extracted from the body text — include backticked commands, file paths, and URLs so the memory fires on the right tool calls.

## When to Use

- User corrects your approach ("don't do X", "always use Y instead") → `--type feedback`
- User confirms a non-obvious approach ("yes exactly", "perfect") → `--type feedback`
- User explicitly asks to remember something → whichever type fits
- You learn about the user's role, preferences, or knowledge → `--type user`
- You learn project facts (deadlines, ownership, decisions, constraints) → `--type project`
- You discover where information lives or how to access it → `--type reference`

## Command

```bash
engram remember "<body>" \
  --type <user|feedback|project|reference> \
  --scope <global|project> \
  [--name "<short name>"] \
  [--description "<one-line summary>"] \
  [--pinned] \
  [--extra-trigger "keyword:foo"] \
  [--extra-trigger "tool_head:Bash:git,push"] \
  [--extra-trigger "path_glob:**/*.py"] \
  [--extra-trigger "error_contains:Bash:ssh:Connection refused"]
```

## Writing Good Bodies

**Include tool patterns in the body so triggers extract automatically:**

Good: "When running `mycli -c`, remember it's a read-only replica — SELECT only, no writes."
→ Extracts: (Bash, mycli) tool_head trigger

Good: "Config for hooks lives in `~/.claude/settings.json` — each entry is {matcher, hooks}."
→ Extracts: path_glob for settings.json

Bad: "The database is read-only." → No tool pattern to bind to.

**Structure for feedback type:**
Lead with the rule, then **Why:** (the reason), then **How to apply:** (when it matters).

## Dry Run

To preview extracted triggers without inserting:
```bash
engram remember --dry-run "<body>"
```

## Defaults

- `--type reference` (most common)
- `--scope project` (scoped to current working directory)
- `--name` auto-synthesized from first line if not provided

## Output

JSON with memory ID, extracted triggers (with existing_memories count for vocabulary convergence), and counts.
