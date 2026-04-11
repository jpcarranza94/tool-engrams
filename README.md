# claude-memory-recall

Tool-bound automatic memory recall for Claude Code.

When Claude is about to run a tool (`git push`, `mycli -c`, `Read config.yml`, etc.), surface memories bound to that exact tool-call pattern — not because a semantic search hopes to match, but because the memory was explicitly filed under that pattern.

## Status

v1 under construction. See `docs/design-v8.md` for the frozen design.

## Architecture at a glance

- **Canonical store:** SQLite at `~/.claude/memory-recall/db.sqlite` (separate from Claude Code's harness-managed memory dir — zero interference).
- **Retrieval:** tiered structural match on `(tool, head_token_sequence)` with path globs, plus a keyword/FTS fallback. Embeddings deferred to v2.
- **Hooks:** `PreToolUse` is the novel surface (memories surface when Claude is about to act), plus `SessionStart`, `UserPromptSubmit`, `PostToolUseFailure`.
- **Brain-like reinforcement:** memories strengthen with use, decay without, and can be forgotten in-session via soft demote.
- **No daemon.** Every hook invocation is a self-contained `memctl` call — SQLite + Python stdlib, zero external dependencies on the hot path.

## Quick CLI

```
memctl pretool              # consumed by PreToolUse hook (reads JSON on stdin)
memctl session-start        # consumed by SessionStart hook
memctl user-prompt          # consumed by UserPromptSubmit hook
memctl post-failure         # consumed by PostToolUseFailure hook
memctl remember <text>      # formation: extract triggers, insert memory
memctl forget <name>        # soft demote; --delete to archive
memctl pin <name>           # pin so recency/usefulness doesn't gate it
memctl recall <query>       # user-facing deep browse (used by /recall skill)
memctl export               # dump to markdown for backup / git snapshot
memctl seed                 # insert a few example memories for smoke testing
```

## Requirements

- Python 3.10+
- Claude Code >= 2.1.59 (for auto-memory + full hook event set)
