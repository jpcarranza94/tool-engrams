# CLAUDE.md

## What this project is

ToolEngrams is a tool-bound memory system for Claude Code. Memories bind to command prefixes (e.g., `git push --force`) and surface automatically via Claude Code hooks when Claude is about to run a matching command.

## Key directories

- `toolengrams/` — core package
  - `commands/` — CLI handlers (one per subcommand: pretool, post_tool, remember, forget, etc.)
  - `prompts/` — all prompt text in one place (session_start, pretool, watcher, consolidation)
  - `consolidation/` — nightly agent (collect sessions, spawn Opus review, launchd schedule)
  - `migrations/` — SQL migration files (auto-discovered by db.py)
  - `watcher/` — event-driven background LLM work with **two roles**: `formation` (creates memories) and `evaluation` (judges how surfaced memories fared). `tick.py` is the core (one read→decide→`claude -p` per event, role-dispatched, plus coalesce policy + the SessionStart idle-sweep); `state.py` is the single seam over `watcher_state`, keyed `(work_session_id, role)` (cursor / armed / fail_streak / last_tick_ts, plus `sweep_idle`); `agent.py` runs the permissioned per-role `claude -p` session (a command allowlist, NOT a JSON schema); `transcript_format.py` the JSONL→text delta; `log.py` the shared log sink. Model via `$ENGRAM_WATCHER_MODEL` (default `sonnet`).
  - `memory_store.py` — the Memory aggregate persistence seam: **every** SQL statement against `memories` / `triggers` / `memories_fts` lives here. Reads return typed `Memory` / `Trigger` objects (models.py); writes (insert/update/delete, reinforcement counter bumps, trigger persistence) go through its named functions. The PreToolUse hot path (`match_token_triggers` / `match_path_triggers`) returns raw rows so rank.py can build the lean `Candidate` with no per-call allocation. Each table has exactly one seam: `memories`/`triggers`/`memories_fts` → `memory_store.py`; `session_surfaces`/`session_turns` → `retrieval/session_state.py`; `consolidation_runs` → `consolidation/runs.py`; `watcher_state` → `watcher/state.py`. (Exception: `cli/migrate_v1_to_v2.py` keeps its own SQL — it reads the OLD v1 schema the stores don't model.)
  - `triggers.py` — trigger extraction validation (delegates trigger writes to memory_store)
  - `formation.py` — pure trigger extraction from memory body text (no DB writes)
- `skills/` — Claude Code skill files (symlinked to ~/.claude/skills/ by install.sh; skill name comes from the folder basename — no frontmatter `name`). A plugin packaging was built and rejected — see `docs/adr/0004`; install.sh is the single install path.
- `tests/` — unit tests + `tests/e2e/` for claude -p integration tests
- `experiments/` — experiment scripts (not production)

## How it works

1. **PreToolUse hook** (`pretool.py`) — fires before every whitelisted tool call. Matches memories whose trigger prefix matches the command; a `block` denies the call and injects its body, a `hint` allows with context. A **surfacing gate** suppresses a `hint` whose noise-aware quality `q` has fallen below 0.5 after warm-up (`block`/`pinned` exempt).
2. **PostToolUse hook** (`post_tool.py`) — increments the per-session turn counter and fires the recovery fast-path tick (formation + eval) on a failure→success. It does **not** credit memories — usefulness is judged by the evaluation watcher (crediting on tool-call success would reinforce any memory that happened to surface).
3. **Scoring** (`reinforcement/scoring.py`) — `q = (useful_count + 1) / (useful_count + noise_count + 2)` (noise-aware, Laplace-smoothed) drives both ranking and the surfacing gate. `useful_count` / `noise_count` are written **only** by the evaluation watcher via `engram judge`; `unused` verdicts enter neither, so situational memories aren't punished. Recency was removed from ranking (event-driven surfacing makes age a backwards signal).
4. **Watcher** (`watcher/tick.py`) — detached `engram watcher-tick` per meaningful event, role-dispatched. **Formation** (`engram remember`) fires on **Stop**, failure→success **recovery**, a likely **UserPromptSubmit** correction, and **SessionEnd/PreCompact** flush; it gates out pure-chat turns unless armed by a prior failure. **Evaluation** (`engram judge`) fires at the Stop/flush **after** a surface, *only when surfaces are pending*, reads the transcript **forward**, judges each surfaced memory `helpful`/`unused`/`noise`, defers by not-judging, and is forced to closure on flush. Each role runs a permissioned `claude -p --resume` that calls the engram CLI itself (a per-role allowlist, no JSON schema; recursion-guarded by `ENGRAM_IN_WATCHER` + an internal cwd). Ticks are coalesced and serialized per `(session, role)`; state lives in `watcher_state` via `watcher/state.py`. **Tail recovery:** the next **SessionStart** idle-sweep re-fires a formation flush (and an eval flush if surfaces are still pending) for an abandoned session.
5. **Consolidation** (`consolidation/agent.py`) — nightly Opus agent that reviews the day's sessions; aggregates per-memory `helpful`/`unused`/`noise` outcomes and prefers **narrowing a noisy trigger** (`engram trigger`, counters preserved) over archiving.
6. **Kill switch** (`pause.py`) — `engram pause`/`resume` toggle a flag file next to the DB; `$ENGRAM_DISABLED` beats the flag (`1` force-off, `0` force-on). Every hook entry point and `watcher/tick.py` check `pause.is_disabled()` first and stand down fail-open. `engram status` reports the state.

## Running tests

```bash
pytest                        # unit tests (fast)
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

- `$ENGRAM_WATCHER_MODEL` — model for the watcher's `claude -p` (default `sonnet`).
- `$ENGRAM_FORMATION_MODEL` / `$ENGRAM_EVAL_MODEL` — per-role model overrides; beat `$ENGRAM_WATCHER_MODEL` for their role.
- `$ENGRAM_WATCHER_TIMEOUT` — per-call `claude -p` timeout in seconds (default `300`).
- `$ENGRAM_TICK_COALESCE_SEC` — min seconds between ticks for one session; a burst of triggers coalesces into one model call (default `45`; flush triggers ignore it).
- `$ENGRAM_IDLE_SWEEP_SEC` — how old a tracked session's last tick must be before the SessionStart idle-sweep treats its unread tail as abandoned and re-fires a flush tick (default `1800`).
- `$ENGRAM_CLEANUP_TTL_SEC` — how cold watcher residue must be before the once-daily `engram cleanup` (spawned detached from SessionStart, marker-gated) reaps it: dead `watcher_state` rows (transcript deleted), stale sandbox cwds, old internal transcript dirs (default `604800` = 7 days).

`MAX_FORM_RETRIES` (tick.py) bounds how many ticks a failed transcript window is retried (cursor held, `fail_streak` persisted in `watcher_state`) before giving up and advancing past it; it is a correctness bound, not an env knob.

## DB location

`~/.claude/tool-engrams/db.sqlite` (override with `$ENGRAM_DB`). Schema in `toolengrams/schema.sql`, migrations in `toolengrams/migrations/`.

## CLI entry point

`toolengrams/__main__.py` → `engram` binary. Subcommands with their own argparse use `_SELF_PARSING` dict for direct dispatch.
