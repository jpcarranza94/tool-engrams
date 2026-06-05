# CLAUDE.md

## What this project is

ToolEngrams is a tool-bound memory system for Claude Code. Memories bind to command prefixes (e.g., `git push --force`) and surface automatically via Claude Code hooks when Claude is about to run a matching command.

## Key directories

- `toolengrams/` — core package
  - `commands/` — CLI handlers (one per subcommand: pretool, post_tool, remember, forget, etc.)
  - `prompts/` — all prompt text in one place (session_start, pretool, watcher, consolidation)
  - `consolidation/` — nightly agent (collect sessions, spawn Opus review, launchd schedule)
  - `migrations/` — SQL migration files (auto-discovered by db.py)
  - `watcher/` — event-driven memory formation. `tick.py` is the core (one read→gate→`claude -p`→save per event, plus coalesce policy + the SessionStart idle-sweep); `state.py` is the single seam over the `watcher_state` table (cursor / armed / fail_streak / last_tick_ts, plus `sweep_idle`); `agent.py` the `claude -p` calls; `transcript_format.py` the JSONL→text delta; `log.py` the shared log sink. Model via `$ENGRAM_WATCHER_MODEL` (default `opus`).
  - `memory_store.py` — the Memory aggregate persistence seam: **every** SQL statement against `memories` / `triggers` / `memories_fts` lives here. Reads return typed `Memory` / `Trigger` objects (models.py); writes (insert/update/delete, reinforcement counter bumps, trigger persistence) go through its named functions. The PreToolUse hot path (`match_token_triggers` / `match_path_triggers`) returns raw rows so rank.py can build the lean `Candidate` with no per-call allocation. (`session_surfaces` / `session_turns` are still owned by `retrieval/session_state.py`; `watcher_state` by `watcher/state.py`.)
  - `triggers.py` — trigger extraction validation (delegates trigger writes to memory_store)
  - `formation.py` — pure trigger extraction from memory body text (no DB writes)
- `skills/` — Claude Code skill files (symlinked to ~/.claude/skills/)
- `tests/` — unit tests + `tests/e2e/` for claude -p integration tests
- `experiments/` — experiment scripts (not production)

## How it works

1. **PreToolUse hook** (`pretool.py`) — fires before every whitelisted tool call. Queries SQLite for memories whose trigger prefix matches the command. Feedback memories deny (block) the call; reference memories allow with context.
2. **PostToolUse hook** (`post_tool.py`) — bumps useful_count on success.
3. **Watcher** (`watcher/tick.py`) — event-driven memory formation. Hooks fire a detached `engram watcher-tick` per meaningful event: **Stop** (turn boundary — the primary trigger), **PostToolUse** failure→success (recovery fast-path), **UserPromptSubmit** on a likely correction, and **SessionEnd/PreCompact** (flush). Each tick reads the JSONL transcript delta since its cursor, gates out pure-chat turns (unless armed by a prior failure), calls `claude -p --resume` (model via `$ENGRAM_WATCHER_MODEL`), and saves. Ticks are serialized per session by a file lock and coalesced by `$ENGRAM_TICK_COALESCE_SEC`. State (cursor / armed / fail_streak / last_tick_ts) lives in `watcher_state`, accessed only through `watcher/state.py`. **Tail recovery:** if a session dies before its final Stop/flush, the next **SessionStart** runs an idle-sweep (`state.sweep_idle`) that re-fires a flush tick for any tracked session with unread lines and an old last tick.
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

Read per tick (each tick is a fresh process), so changes apply to the next event:

- `$ENGRAM_WATCHER_MODEL` — model for the watcher's `claude -p` (default `opus`).
- `$ENGRAM_WATCHER_TIMEOUT` — per-call `claude -p` timeout in seconds (default `120`).
- `$ENGRAM_TICK_COALESCE_SEC` — min seconds between ticks for one session; a burst of triggers coalesces into one model call (default `45`; flush triggers ignore it).
- `$ENGRAM_IDLE_SWEEP_SEC` — how old a tracked session's last tick must be before the SessionStart idle-sweep treats its unread tail as abandoned and re-fires a flush tick (default `1800`).

`MAX_FORM_RETRIES` (tick.py) bounds how many ticks a failed transcript window is retried (cursor held, `fail_streak` persisted in `watcher_state`) before giving up and advancing past it; it is a correctness bound, not an env knob.

## DB location

`~/.claude/tool-engrams/db.sqlite` (override with `$ENGRAM_DB`). Schema in `toolengrams/schema.sql`, migrations in `toolengrams/migrations/`.

## CLI entry point

`toolengrams/__main__.py` → `engram` binary. Subcommands with their own argparse use `_SELF_PARSING` dict for direct dispatch.
