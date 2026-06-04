# CLAUDE.md

## What this project is

ToolEngrams is a tool-bound memory system for Claude Code. Memories bind to command prefixes (e.g., `git push --force`) and surface automatically via Claude Code hooks when Claude is about to run a matching command.

## Key directories

- `toolengrams/` — core package
  - `commands/` — CLI handlers (one per subcommand: pretool, post_tool, remember, forget, etc.)
  - `prompts/` — all prompt text in one place (session_start, pretool, watcher, consolidation)
  - `consolidation/` — nightly agent (collect sessions, spawn Opus review, launchd schedule)
  - `migrations/` — SQL migration files (auto-discovered by db.py)
  - `watcher.py` — persistent parallel `claude -p` session (model via `$ENGRAM_WATCHER_MODEL`, default `opus`); spawned by session_start, not a user command
  - `triggers.py` — trigger persistence (shared by remember, dedup)
  - `formation.py` — pure trigger extraction from memory body text (no DB writes)
- `skills/` — Claude Code skill files (symlinked to ~/.claude/skills/)
- `tests/` — unit tests + `tests/e2e/` for claude -p integration tests
- `experiments/` — experiment scripts (not production)

## How it works

1. **PreToolUse hook** (`pretool.py`) — fires before every whitelisted tool call. Queries SQLite for memories whose trigger prefix matches the command. Feedback memories deny (block) the call; reference memories allow with context.
2. **PostToolUse hook** (`post_tool.py`) — bumps useful_count on success.
3. **Watcher** (`watcher.py`) — persistent parallel `claude -p` session (model via `$ENGRAM_WATCHER_MODEL`, default `opus`) that wakes every 5 min, reads the JSONL transcript delta, and forms memories from multi-call patterns. Spawned by SessionStart, respawned by UserPromptSubmit if dead.
4. **Consolidation** (`consolidation/agent.py`) — nightly Opus agent that reviews the day's sessions.

## Running tests

```bash
pytest                        # unit tests (133, fast)
pytest tests/e2e/ -m e2e     # E2E tests (spawns claude -p, slow)
```

## Code style

- Python 3.10+, no external dependencies on the hot path (stdlib + sqlite3 only)
- All imports at module level, never inline
- Hooks must be fail-open (try/except → exit 0 with empty JSON)
- PreToolUse latency budget: single-digit ms for DB queries
- Prompts live in `toolengrams/prompts/`, not inline in command files

## Watcher tunables (env)

Read at spawn time, so changes apply to newly-spawned watchers (the running ones hold their spawn-time env until they respawn):

- `$ENGRAM_WATCHER_MODEL` — model for the watcher's `claude -p` (default `opus`).
- `$ENGRAM_WATCHER_TIMEOUT` — per-call `claude -p` timeout in seconds (default `120`).

`MAX_FORM_RETRIES` (lifecycle.py) bounds how many ticks a failed transcript window is retried before the watcher gives up and advances past it; it is a correctness bound, not an env knob.

## DB location

`~/.claude/tool-engrams/db.sqlite` (override with `$ENGRAM_DB`). Schema in `toolengrams/schema.sql`, migrations in `toolengrams/migrations/`.

## CLI entry point

`toolengrams/__main__.py` → `engram` binary. Subcommands with their own argparse use `_SELF_PARSING` dict for direct dispatch.
