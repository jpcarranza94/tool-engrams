# CLAUDE.md

## What this project is

ToolEngrams is a tool-bound memory system for coding agents. Memories bind to command prefixes (e.g., `git push --force`) and surface automatically through target harness hooks when the agent is about to run a matching command. The hooked **target** agent and the background **engine** runner are separate adapters; Claude Code and Codex can share one DB.

## Key directories

- `toolengrams/` — core package
  - `cli/` — user-facing CLI handlers (one per subcommand: remember, forget, recall, judge, doctor, seed, status, etc.)
  - `hooks/` — target hook handlers (pretool, post_tool, post_tool_failure, session_start, user_prompt, stop, flush); shared failure-moment surfacing lives in `hooks/_failure_surface.py`
  - `prompts/` — all prompt text in one place (session_start, pretool, watcher, consolidation)
  - `consolidation/` — nightly agent (spawn selected engine review, launchd schedule; the session collector lives behind the target adapters)
  - `migrations/` — SQL migration files (auto-discovered by db.py)
  - `watcher/` — event-driven background LLM work with **two roles**: `formation` (creates memories) and `evaluation` (judges how surfaced memories fared). `tick.py` is the core (one read→decide→engine call per event, role-dispatched, plus coalesce policy + the SessionStart idle-sweep); `state.py` is the single seam over `watcher_state`, keyed `(work_session_id, role)` (cursor / armed / fail_streak / last_tick_ts / `target`, plus `sweep_idle`; no resume id — ticks are stateless per ADR-0005); `agent.py` runs the fresh permissioned per-role engine session (a command allowlist + `$ENGRAM_ALLOWED_VERBS`, NOT a JSON schema); `transcript_io.py` the format-agnostic cursor reads (the JSONL→text parser lives in the target adapter); `log.py` the shared log sink. Model via `$ENGRAM_WATCHER_MODEL` (default `sonnet`).
  - `memory_store.py` — the Memory aggregate persistence seam: **every** SQL statement against `memories` / `triggers` / `memories_fts` lives here. Reads return typed `Memory` / `Trigger` objects (models.py); writes (insert/update/delete, reinforcement counter bumps, trigger persistence) go through its named functions. The PreToolUse hot path (`match_token_triggers` / `match_path_triggers`) returns raw rows so rank.py can build the lean `Candidate` with no per-call allocation. Each table has exactly one seam: `memories`/`triggers`/`memories_fts` → `memory_store.py`; `session_surfaces`/`session_turns` → `retrieval/session_state.py`; `consolidation_runs` → `consolidation/runs.py`; `watcher_state` → `watcher/state.py`. (Exception: `cli/migrate_v1_to_v2.py` keeps its own SQL — it reads the OLD v1 schema the stores don't model.)
  - `triggers.py` — trigger extraction validation (delegates trigger writes to memory_store)
  - `target/` — swappable hooked-harness adapters (the agent memories surface into). `interface.py` defines the contract (`TargetAdapter` Protocol: `tool_whitelist`, `has_failure_event`, `extract_hints`, `detect_failure`, `transcript_path` payload-first, `format_delta` emitting the canonical `USER:`/`AGENT:`/`TOOL (X):`/`RESULT:` delta vocabulary, `collect_sessions`, `hook_markers`/`hook_status` for doctor); adapters are plain modules in `TARGETS` (`claude_code/` and `codex/`). Selection: the `--target` flag baked into each wired hook command at install time (never payload-sniffed; several targets can share one DB); `watcher_state.target` tells a detached tick which parser to use. Nightly consolidation collects sessions from every wired target and tags each session by target.
  - `engine/` — swappable headless-runner adapters for background LLM work (watcher ticks, consolidation). `interface.py` defines the contract (`EngineAdapter` Protocol, `EngineRequest`, `SandboxSpec`, `EngineResult`); adapters are plain modules registered in `selection.ENGINES` (`claude_code.py` and `codex.py`). Selection precedence: per-call override → `$ENGRAM_ENGINE` → `engine` key in `<engram home>/config.json` → `claude-code`. Containment is two-layer: the adapter's native sandbox (`prepare_sandbox`; claude-code writes a `.claude/settings.local.json` allowlist, codex uses `codex exec -c` runtime sandbox overrides) plus the engine-agnostic `$ENGRAM_ALLOWED_VERBS` dispatch guard in `__main__.py` (watcher children: formation=`remember`, eval=`judge,quarantine`).
  - `paths.py` — data-home resolution seam (`engram_home()`): `$ENGRAM_HOME` → `~/.tool-engrams` → legacy `~/.claude/tool-engrams`; everything persistent (DB, watcher log, sandboxes, prompt overrides, pause flag) routes through it
  - `formation.py` — pure trigger extraction from memory body text (no DB writes)
- `skills/` — Claude Code skill files (symlinked to ~/.claude/skills/ by install.sh; skill name comes from the folder basename — no frontmatter `name`). A plugin packaging was built and rejected — see `docs/adr/0004`; install.sh is the single install path.
- `tests/` — unit tests + `tests/e2e/` for claude -p integration tests
- `experiments/` — experiment scripts (not production)

## How it works

1. **PreToolUse hook** (`pretool.py`) — fires before every whitelisted tool call. Matches memories whose trigger prefix matches the command; a `block` denies the call and injects its body, a `hint` injects context with **no permissionDecision** (an explicit "allow" would bypass the user's permission prompts — hints must never grant approval). A **surfacing gate** suppresses a `hint` whose noise-aware quality `q` has fallen below 0.5 after warm-up (`block`/`pinned` exempt). **Same-session suppression** (ADR-0006): a hint never surfaces into the session recorded as its `origin_session_id` (blocks exempt; manual saves have NULL origin).
2. **PostToolUse hook** (`post_tool.py`) — increments the per-session turn counter and fires the recovery fast-path tick (formation + eval) on a failure→success. Targets without a dedicated failure event (Codex) surface failure-path hints inline from this hook through `hooks/_failure_surface.py`. It does **not** credit memories — usefulness is judged by the evaluation watcher (crediting on tool-call success would reinforce any memory that happened to surface).
3. **Scoring** (`reinforcement/scoring.py`) — `q = (useful_count + 1) / (useful_count + noise_count + 2)` (noise-aware, Laplace-smoothed) drives both ranking and the surfacing gate. `useful_count` / `noise_count` are written **only** by the evaluation watcher via `engram judge`; `unused` verdicts enter neither, so situational memories aren't punished. Recency was removed from ranking (event-driven surfacing makes age a backwards signal).
4. **Watcher** (`watcher/tick.py`) — detached `engram watcher-tick` per meaningful event, role-dispatched. **Formation** (`engram remember`) fires on **Stop**, failure→success **recovery**, a likely **UserPromptSubmit** correction, and **SessionEnd/PreCompact** flush; it gates out pure-chat turns unless armed by a prior failure. **Evaluation** (`engram judge`) fires at the Stop/flush **after** a surface, *only when surfaces are pending*, reads the transcript **forward**, judges each surfaced memory `helpful`/`unused`/`noise`, defers by not-judging, and is forced to closure on flush. Each role runs a FRESH permissioned engine session per tick (ADR-0005 — no `--resume`; formation re-supplies cross-tick context explicitly: prior-window tail ≤4k chars via `watcher_runs` cursor spans + the session's prior saves from `watcher_run_events`; `ENGRAM_ORIGIN_SESSION` in the child env attributes saves for ADR-0006 suppression). The per-role allowlist is the containment (no JSON schema; eval also gets `engram quarantine` — ADR-0007; recursion-guarded by `ENGRAM_IN_WATCHER` + an internal cwd). Ticks are coalesced and serialized per `(session, role)`; state lives in `watcher_state` via `watcher/state.py`. **Tail recovery:** the next **SessionStart** idle-sweep re-fires a formation flush (and an eval flush if surfaces are still pending) for an abandoned session.
5. **Consolidation** (`consolidation/agent.py`) — nightly engine agent that reviews the day's wired target sessions; aggregates per-memory `helpful`/`unused`/`noise` outcomes and prefers **narrowing a noisy trigger** (`engram trigger`) or **repairing a stale body in place** (`engram edit`) — both counter-preserving — over archiving; it also reviews the eval watcher's quarantines (restore / repair / confirm).
6. **Kill switch** (`pause.py`) — `engram pause`/`resume` toggle a flag file next to the DB; `$ENGRAM_DISABLED` beats the flag (`1` force-off, `0` force-on). Every hook entry point and `watcher/tick.py` check `pause.is_disabled()` first and stand down fail-open. `engram status` reports the state.
7. **Doctor** (`cli/doctor.py`) — `engram doctor` checks plumbing (target hooks wired, `engram` on PATH, target/engine CLI versions, DB schema, kill switch) plus liveness with zero extra writes: max `session_turns.updated_ts` = last hook fire, max `watcher_state.last_tick_ts` = last watcher tick. WARN-only (fresh-but-quiet install) exits 0; any FAIL exits 1. install.sh step 4 runs it. Related: `$ENGRAM_SURFACE_NOTICE=1` makes surfacing hooks emit a visible `systemMessage` when memories surface (smoke tests, surfacing debugging).

## Running tests

```bash
pytest                        # unit tests (fast)
pytest tests/e2e/ -m e2e        # E2E tests (spawns claude -p, slow)
pytest tests/e2e/ -m e2e_codex  # Codex E2E tests (spawns codex, slow)
```

## Code style

- Python 3.10+, no external dependencies on the hot path (stdlib + sqlite3 only)
- All imports at module level, never inline
- Hooks must be fail-open (try/except → exit 0 with empty JSON)
- PreToolUse latency budget: single-digit ms for DB queries
- Prompts live in `toolengrams/prompts/`, not inline in command files

## Watcher tunables (env)

Read per tick (each tick is a fresh process), so changes apply to the next event:

- `$ENGRAM_ENGINE` — which engine adapter runs background work (default `claude-code`; precedence: env → `<home>/config.json` → default).
- `$ENGRAM_WATCHER_MODEL` — model for the watcher's Claude Code engine call (default `sonnet`).
- `$ENGRAM_FORMATION_MODEL` / `$ENGRAM_EVAL_MODEL` — per-role model overrides; beat `$ENGRAM_WATCHER_MODEL` for their role.
- `$ENGRAM_CODEX_WATCHER_MODEL` / `$ENGRAM_CODEX_FORMATION_MODEL` / `$ENGRAM_CODEX_EVAL_MODEL` — Codex engine model overrides; unset lets Codex use its config defaults.
- `$ENGRAM_WATCHER_TIMEOUT` — per-call engine timeout in seconds (default `300`).
- `$ENGRAM_TICK_COALESCE_SEC` — min seconds between ticks for one session; a burst of triggers coalesces into one model call (default `45`; flush triggers ignore it).
- `$ENGRAM_IDLE_SWEEP_SEC` — how old a tracked session's last tick must be before the SessionStart idle-sweep treats its unread tail as abandoned and re-fires a flush tick (default `1800`).
- `$ENGRAM_CLEANUP_TTL_SEC` — how cold watcher residue must be before the once-daily `engram cleanup` (spawned detached from SessionStart, marker-gated) reaps it: dead `watcher_state` rows (transcript deleted), stale sandbox cwds, old internal transcript dirs (default `604800` = 7 days).

`MAX_FORM_RETRIES` (tick.py) bounds how many ticks a failed transcript window is retried (cursor held, `fail_streak` persisted in `watcher_state`) before giving up and advancing past it; it is a correctness bound, not an env knob.

## Data home & DB location

All persistent state lives under the engram home, resolved by `toolengrams/paths.py`: `$ENGRAM_HOME` → `~/.tool-engrams/` (neutral default) → `~/.claude/tool-engrams/` (legacy fallback; install.sh migrates it and leaves a symlink). The DB is `<home>/db.sqlite` (override the file directly with `$ENGRAM_DB`). Schema in `toolengrams/schema.sql`, migrations in `toolengrams/migrations/`.

## CLI entry point

`toolengrams/__main__.py` → `engram` binary. Subcommands with their own argparse use `_SELF_PARSING` dict for direct dispatch.
