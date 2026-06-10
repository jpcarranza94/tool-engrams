---
name: engram-remember
description: "Save a tool-bound memory to ToolEngrams. Use ONLY for facts about how to use specific commands, CLIs, or files."
---

# ToolEngrams: Remember

Save a tool-bound memory. Use `--trigger` to specify the exact command prefix the memory should fire on.

## When to Use

- User corrects how to use a command ("don't use pip, use uv pip install") → `--kind hint`
- User confirms a non-obvious tool usage ("yes, always --build with docker compose") → `--kind hint`
- A rule that must be enforced before a command runs ("never plain --force") → `--kind block`

## When NOT to Use

Do NOT save user preferences, project deadlines, team info, or anything that doesn't bind to a tool call. Those belong in Claude's built-in memory system.

## Command

```bash
engram remember "<body>" \
  --trigger "<command prefix>" \
  --kind <block|hint> \
  --scope <global|project> \
  [--name "<short name>"] \
  [--path "**/*.py"]
```

## Examples

```bash
# Rule: block git push --force, suggest --force-with-lease
engram remember "Use --force-with-lease instead of --force to avoid overwriting others' work" \
  --trigger "git push --force" \
  --trigger "git push -f" \
  --kind block --name "git-force-with-lease"

# Hint: context when psql -h replica fails
engram remember "psql -h replica connects to a read-only replica. SELECT only, no writes." \
  --trigger "psql -h replica" \
  --kind hint --name "psql-replica-read-only"

# Path-based: fire when editing Python test files
engram remember "Use pytest.raises as context manager, never decorator form" \
  --path "**/test_*.py" \
  --kind hint --name "pytest-raises-context-manager"
```

## Triggers

`--trigger` specifies a command prefix. The memory surfaces when Claude runs a Bash command that starts with that prefix:
- `--trigger "git push --force"` matches `git push --force origin main`
- `--trigger "docker compose up"` matches `docker compose up --build -d`
- `--trigger "ssh -i ~/.ssh/aws"` matches `ssh -i ~/.ssh/aws-ec2 user@host`

`--path` specifies a file glob. The memory surfaces when Claude Reads/Edits/Writes a matching file.

If neither `--trigger` nor `--path` is provided, triggers are auto-extracted from backticked commands in the body (fallback).

## Kinds

- `--kind block` — enforced rules. **Denies the tool call** at PreToolUse and injects the memory body so Claude retries correctly. Rare; use for must-not-happen patterns.
- `--kind hint` — informational (default). Injected as context at PostToolUseFailure when a matching tool call fails (no blocking).

## Dedup

If an existing memory already has overlapping triggers, the body is updated instead of creating a duplicate.
