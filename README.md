# ToolEngrams

Tool-bound automatic memory recall for Claude Code.

When Claude is about to run a tool (`git push`, `mycli -c`, `Read config.yml`, etc.), surface memories bound to that exact tool-call pattern — not because a semantic search hopes to match, but because the memory was explicitly filed under that pattern, strengthened every time it helped, and decayed when it didn't.

The name is literal: an **engram** is the physical substrate of a memory in neuroscience — the biological trace left by experience. ToolEngrams are memory traces bound to tool-call patterns, reinforced with use, forgotten on request.

## Status

v1 under construction. See `docs/design-v8.md` for the frozen design.

## What makes this different

Most prior art for agent memory (claude-mem, memsearch, rag-cli, etc.) indexes memory by content and retrieves by semantic similarity to the prompt — a "search across everything you know" model. ToolEngrams inverts that:

1. **Memories are bound to tool-call patterns at write time**, not searched at read time. Every memory answers "when should Claude see this?" explicitly.
2. **Retrieval happens mid-turn, not at the prompt boundary.** When Claude is about to call `mycli -c "..."`, the hook retrieves memories bound to `(Bash, mycli)` and injects them before the tool dispatches.
3. **Brain-like reinforcement.** Memories strengthen with use (`useful_count`), decay without (`last_surfaced_ts` + half-life), and can be forgotten in-session via soft demote.
4. **Self-organizing vocabulary.** New memories cluster around existing tool-call touchpoints via a vocabulary-consolidation step at formation time, so the binding vocabulary converges on what actually matters in your work.

## Architecture at a glance

- **Canonical store:** SQLite at `~/.claude/tool-engrams/db.sqlite` — fully separate from Claude Code's harness-managed memory dir, zero interference.
- **Retrieval:** tiered structural match on `(tool, head_token_sequence)` with path globs, plus keyword/FTS fallback. Embeddings deferred to v2.
- **Hooks:** `PreToolUse` is the novel surface (memories surface when Claude is about to act), plus `SessionStart`, `UserPromptSubmit`, `PostToolUse` (failure subset).
- **Relevance filter:** per-cluster Laplace-smoothed threshold with an absolute floor — handles cold-start cleanly without hand-tuned global constants.
- **No daemon.** Every hook invocation is a self-contained `engram` call — SQLite + Python stdlib, zero external dependencies on the hot path.

## CLI surface

```
engram pretool              # consumed by PreToolUse hook (reads JSON on stdin)
engram session-start        # consumed by SessionStart hook
engram user-prompt          # consumed by UserPromptSubmit hook
engram post-failure         # consumed by PostToolUse hook (failure subset)
engram remember <text>      # formation: extract triggers, insert memory [v1.5]
engram forget <name>        # soft demote; --delete to archive [v1.5]
engram pin <name>           # pin so reinforcement doesn't gate it [v1.5]
engram recall <query>       # user-facing deep browse [v1.5]
engram export               # dump to markdown for backup / git snapshot [v1.5]
engram seed                 # insert a few example memories for smoke testing
```

## Testing

```bash
# Unit tests (fast, deterministic)
pytest tests/

# End-to-end tests (slow, spawns real `claude -p`, opt-in)
pytest tests/e2e/ -m e2e
```

Every hook has an end-to-end test that seeds a memory in an isolated SQLite DB, wires the hook via a temp `.claude/settings.local.json`, and spawns `claude -p` to verify the memory actually reaches Claude's context window at runtime.

## Requirements

- Python 3.10+
- Claude Code >= 2.1.59 (for auto-memory + full hook event set)
