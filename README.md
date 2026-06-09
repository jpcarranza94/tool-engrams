# ToolEngrams

**Tool-bound memory for Claude Code.** Agent-facing tools become self-documenting through interaction: Claude fails a call, the system remembers why, and next session (or next month) arrives with that knowledge in hand.

> **Status:** alpha. Breaking changes expected; no stable users to protect. See `docs/design.md` for the design (and `docs/adr/` for the load-bearing decisions).

## The problem

Claude Code is great at well-known CLIs. It's not great at:

- **Custom CLIs** not in training data (your company's internal tools, bespoke wrappers)
- **Project-specific conventions** bound to commands ("this repo's test command needs REUSE_DB=1", "deploy requires cd into frontend/ first")
- **Subtle semantic gotchas** in databases, APIs, and frameworks (the wrong column name, the surprising flag, the workaround only the tribe knows)

For all of these, the useful information is bound to a specific tool-call pattern. Generic RAG / conversational memory doesn't help — the agent needs the fact *right when it's about to act*.

## System overview

Hooks talk to Claude Code on the hot path; two background LLM roles (formation + evaluation) and a nightly agent run out-of-band. All share one SQLite DB.

```
                      Claude Code
         ┌─────────────────────────────────────┐
         │  tool call  │   hooks pipe JSON     │
         ▼             ▼                       ▼
   ┌──────────┐ ┌──────────────┐  ┌─────────────────────┐
   │ pretool  │ │  post-tool-  │  │ stop / flush /      │
   │ block +  │ │  failure     │  │ post-tool / prompt  │
   │ hint     │ │  (hint+arm)  │  │ (tick triggers)     │
   │ (+gate)  │ │  (+gate)     │  │                     │
   └────┬─────┘ └──────┬───────┘  └──────────┬──────────┘
        │              │                     │ fires detached ticks
        │              │              ┌──────┴───────┐
        │              │              ▼              ▼
        │              │     ┌───────────────┐ ┌───────────────┐
        │              │     │ formation tick│ │ eval tick     │
        │              │     │ `engram       │ │ `engram judge`│
        │              │     │  remember`    │ │ (if surfaces  │
        │              │     │               │ │  pending)     │
        │              │     └──────┬────────┘ └──────┬────────┘
        ▼              ▼            ▼                 ▼
   ┌────────────────────────────────────────────────────┐
   │               SQLite  (~/.claude/tool-engrams/)    │
   │  memories │ triggers │ session_surfaces │ ...      │
   └────────────────────────────────────────────────────┘
                           ▲
                           │ nightly read+prune+narrow+discover
                  ┌────────┴──────────┐
                  │ consolidation     │
                  │ (Opus, daily)     │
                  └───────────────────┘
```

No network and no LLM on the hot path (hooks are single-digit-ms SQL lookups). Both watcher roles and consolidation are permissioned `claude -p` sessions that do their job by **calling the `engram` CLI** — there is no constrained JSON schema to parse (see `docs/adr/0001`).

| Component | Code | Runs when | Role |
|---|---|---|---|
| **PreToolUse hook** | `toolengrams/hooks/pretool.py` | Before every whitelisted tool call | Surfaces matching memories: a `block` denies the call + injects its body; a `hint` allows + injects context. The **surfacing gate** drops a hint whose quality `q` has fallen below 0.5. |
| **PostToolUse hook** | `toolengrams/hooks/post_tool.py` | After a tool call | Increments the per-session turn counter and fires the recovery fast-path tick on a failure→success. **Does not credit usefulness** — that moved to the evaluation watcher. |
| **PostToolUseFailure hook** | `toolengrams/hooks/post_tool_failure.py` | After a real tool failure | Surfaces `hint`-kind memories as `additionalContext` (gated by `q`); arms the formation watcher. |
| **Formation watcher** | `toolengrams/watcher/tick.py` (role `formation`) | Detached `claude -p` per turn (Stop), recovery, correction, or session-end flush | Reads the JSONL transcript delta and calls `engram remember` for new patterns. |
| **Evaluation watcher** | `toolengrams/watcher/tick.py` (role `eval`) | Detached `claude -p` at the Stop/flush **after** a surface, only when surfaces are pending | Reads forward, judges how each surfaced memory fared, and calls `engram judge`. |
| **Consolidation** | `toolengrams/consolidation/agent.py` | Nightly (launchd/cron) | Opus agent reviews yesterday's sessions — prunes noise, **narrows over-matching triggers**, discovers missed patterns, deduplicates. |

## Surfacing pipeline

When Claude is about to run a tool call, this is the full path from input → injected memory. The pipeline is the same in PreToolUse and PostToolUseFailure — just a different `kind` filter and output field.

```
tool call JSON on stdin
         │
         ▼
┌─────────────────────────┐   e.g. Bash "git push --force origin main"
│ 1. extract tokens/paths │     → tokens = ["git","push","--force","origin","main"]
│    (retrieval/extract)  │     → paths  = []
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐   single indexed lookup per call:
│ 2. index lookup by      │     SELECT … FROM triggers WHERE
│    first_token          │       first_token = 'git' AND kind='token_subseq'
│    (retrieval/rank)     │     → small candidate bucket
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐   for each candidate, check in Python:
│ 3. subsequence filter   │     is ['git','push','--force'] a subseq of
│    (retrieval/rank)     │     ['git','push','--force','origin','main']? yes
└──────────┬──────────────┘
           │  + path_glob matches (fnmatch) for Read/Edit/etc.
           ▼
┌─────────────────────────┐   final_score = (0.5 + q) × (1.5 if pinned)
│ 4. score each candidate │   q = noise-aware usefulness (see below)
│    (reinforcement/      │
│     scoring.py)         │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐   suppress a hint with q < 0.5 (after warm-up);
│ 5. surfacing gate       │   block + pinned are exempt
│    (scoring.is_gated)   │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐   drop memories already surfaced this session
│ 6. session dedup        │   (memory_id already in session_surfaces)
│    (retrieval/          │
│     session_state)      │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐   kind filter (pretool keeps both; post-failure
│ 7. kind + specificity   │   keeps hints), then sort by
│    sort + cap           │   (longest matching trigger DESC, score DESC), cap N
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐   log to session_surfaces (outcome=NULL),
│ 8. log + bump + emit    │   bump surface_count, emit additionalContext
│                         │   (+ deny for blocks). The eval watcher fills in
│                         │   outcome later.
└─────────────────────────┘
```

### Why subsequence match

A trigger is a list of required tokens in order. The tool call matches if all trigger tokens appear in the tokenization in the same order — **gaps allowed**.

```
trigger:   ["ergeon", "order", "reassign"]
matches:   ergeon order 12345 reassign
matches:   ergeon --env staging order abc reassign --reason X
no match:  ergeon reassign order    (wrong order)
no match:  ergeon customer reassign (missing "order")
```

Simple prefix matching breaks for real CLIs because positional IDs sit between verbs: `gh pr 123 comment`, `kubectl get pod-abc123 describe`, `jira sprint 5 add`. Subsequence match handles them naturally.

### Two kinds of memory, two surface moments

| kind | Fires at | Effect | When to author |
|---|---|---|---|
| **hint** | `PreToolUse` (allow + context) and `PostToolUseFailure` (after a real failure) | Injects body as `additionalContext`, non-blocking | Default. Most discoveries land here. |
| **block** | `PreToolUse` (before every whitelisted call) | **Denies** the call + injects body. The deny is invisible to the user; it fails the call *for Claude* and prompts an in-loop retry with context. | Rare. Only for things you actively want to prevent (destructive ops, silently-wrong output). |

Most users will author zero `block` memories. Hints carry the weight. Blocks and pinned memories are **exempt from the surfacing gate** — a safety rule that's rarely visibly-heeded must still fire.

## Memory formation

Memories are born three ways, in decreasing automation. All converge at `toolengrams/formation/triggers.py` (`insert_candidate_triggers`), which validates the first token and writes the `first_token` + `tokens_json` rows the surfacing pipeline reads.

| Source | Code | How |
|---|---|---|
| **Formation watcher** (background `claude -p`) | `toolengrams/watcher/` + `toolengrams/prompts/defaults/watcher.md` | A permissioned session (model via `$ENGRAM_WATCHER_MODEL`, default opus) whose only allowed command is `engram remember`. It reads the transcript delta and runs `engram remember …` for patterns worth keeping — native tool-calling, no JSON schema. |
| **Consolidation** (Opus nightly) | `toolengrams/consolidation/agent.py` + `toolengrams/prompts/defaults/consolidation.md` | Opus issues `engram remember / forget / trigger` commands directly after reviewing yesterday's sessions. |
| **Manual** | `toolengrams/cli/remember.py` | `--trigger "<tokens>"` / `--path "<glob>"` flags, or triggers extracted from the body via `formation/candidates.py` (backticked shell snippets, paths, URLs). |

```bash
# Manual example — the watcher would run the same command automatically
engram remember \
  "Use --force-with-lease; --force overwrites co-workers' pushed commits." \
  --kind block --scope global \
  --trigger "git push --force" \
  --trigger "git push -f"
```

The watcher, evaluation, and consolidation prompts are **user-overridable** without forking — see "Configurable prompts" below.

## Reinforcement loop

A memory's quality is judged by the **evaluation watcher** reading the transcript — not inferred from whether the tool call succeeded. (Most calls succeed, so crediting on success would reinforce any memory that happened to surface — especially a path-glob memory that fires on every matching file read.)

Three counters live on each memory row:

| Counter | Written by | Meaning |
|---|---|---|
| `surface_count` | `pretool` / `post-tool-failure` (`memory_store.bump_surface`) | Times the memory was shown. **Telemetry only** now — not a quality signal. |
| `useful_count` | the eval watcher via `engram judge … helpful` | The model visibly followed / used the memory. |
| `noise_count` | the eval watcher via `engram judge … noise` | The memory had no bearing on the call — the trigger over-matched. |

The eval watcher reads the transcript *after* a surface and records one of three verdicts per surfaced memory: **`helpful`** (`useful_count++`), **`unused`** (relevant but not acted on — counts toward *neither*), or **`noise`** (`noise_count++`). A surface it can't yet conclude is simply left unjudged and re-listed next pass; the session-end flush forces closure.

A single noise-aware, Laplace-smoothed ratio drives both ranking and the gate:

```
q = (useful_count + 1) / (useful_count + noise_count + 2)     # 0.5 at cold start
final_score = (0.5 + q) × (1.5 if pinned)
```

Because `unused` enters neither counter, a correct-but-situational memory keeps a high `q`. The **surfacing gate** suppresses a `hint` once `q < 0.5` (after a warm-up of `useful_count + noise_count ≥ 3`) — it has proven more noise than signal. The 0.5 threshold is the prior's mean, not a tuned constant. `block` and `pinned` memories are exempt.

**Recency was removed from ranking.** Surfacing is event-driven: a memory's last-surfaced time is old precisely when its trigger hasn't fired — and the moment it fires again it's relevant *now*. Punishing rarity is backwards for a memory system. Staleness (a memory whose *content* is wrong) is consolidation's job, via a git-aware audit.

## Code layout

```
toolengrams/
├── hooks/             ← one file per Claude Code hook event
│   ├── pretool.py             block + hint surfacing, deny, q-gate
│   ├── post_tool.py           turn counter + recovery tick (no crediting)
│   ├── post_tool_failure.py   hint surfacing (non-blocking) + arm
│   ├── session_start.py       session tracking + idle-sweep
│   ├── user_prompt.py         fires a formation tick on a likely correction
│   ├── stop.py                primary tick trigger (formation + eval)
│   └── flush.py               final tick (SessionEnd / PreCompact)
├── retrieval/         ← read path (tool call → memories)
│   ├── extract.py             tool payload → (tokens, paths)
│   ├── rank.py                candidates, subseq match, score
│   └── session_state.py       session_surfaces + session_turns + pending surfaces
├── formation/         ← write path (body → memory + triggers)
│   ├── candidates.py          extract triggers from a markdown body
│   ├── triggers.py            validate first token + write to `triggers`
│   ├── dedup.py               merge new memories into overlapping ones
│   └── secrets.py             reject bodies containing API keys etc.
├── reinforcement/
│   └── scoring.py             q (noise-aware usefulness), final_score, is_gated (the gate)
├── consolidation/     ← nightly Opus agent (+ runs.py seam, schedule)
├── prompts/
│   ├── defaults/              shipped prompts: watcher.md, eval.md, consolidation.md
│   ├── loader.py              env-var / user-override / default lookup chain
│   ├── watcher.py             formation prompt loader
│   ├── eval.py                evaluation prompt loader
│   └── consolidation.py       consolidation prompt loader
├── cli/               ← user-facing subcommands (remember, recall, judge, trigger, …)
├── migrations/        ← SQL schema evolution (v*.sql)
├── schema.sql         ← complete v_latest snapshot for fresh DBs
├── db.py              ← connection + migration runner
├── models.py          ← dataclasses (Memory, Trigger, Candidate, …)
├── memory_store.py    ← Memory aggregate seam: all memories/triggers/FTS SQL + counter bumps
└── watcher/           ← event-driven formation + evaluation
    ├── tick.py                role-dispatched tick engine + coalesce + idle-sweep
    ├── agent.py               permissioned per-role `claude -p` session runner
    ├── state.py               watcher_state seam, keyed (work_session_id, role)
    ├── transcript_format.py   JSONL → readable delta
    └── log.py                 shared log sink
```

Each table has exactly one persistence seam: `memories`/`triggers`/`memories_fts` → `memory_store.py`; `session_surfaces`/`session_turns` → `retrieval/session_state.py`; `consolidation_runs` → `consolidation/runs.py`; `watcher_state` → `watcher/state.py`.

The hot path (hooks) has **no external dependencies** — stdlib + sqlite3 only. The one runtime dependency, `rich`, is used solely by the `engram monitor` dashboard and is imported lazily there, so hooks never load it. LLMs run only in the watcher ticks and the consolidation agent, all out-of-band from the tool-call path.

## Install

```bash
git clone https://github.com/jpcarranza94/tool-engrams.git
cd tool-engrams
./install.sh
```

The installer:

1. Installs `toolengrams` (pip editable)
2. Wires hooks into `~/.claude/settings.json`:
   - `SessionStart` (session tracking + idle-sweep), `UserPromptSubmit` (formation tick on correction)
   - `Stop` (formation + eval tick triggers), `SessionEnd`/`PreCompact` (flush ticks)
   - `PreToolUse` (block + hint surfacing)
   - `PostToolUse` (turn counter + recovery tick)
   - `PostToolUseFailure` (hint surfacing + arms the watcher)
3. Symlinks skills (`/engram-remember`, `/engram-forget`, `/engram-recall`)
4. Initializes the SQLite DB at `~/.claude/tool-engrams/db.sqlite`
5. Optionally schedules the nightly consolidation agent

### Requirements

- Python 3.10+ (hot path is stdlib + sqlite3; `rich` is the only runtime dep, used by the dashboard)
- Claude Code ≥ 2.1.117 (needs the `PostToolUseFailure` hook event)

## CLI

```
engram recall [query]             Browse and search memories
engram recall --id N              Full detail on one memory
engram recall --stats             Summary counts by kind/scope
engram remember "<body>" \
  --kind <block|hint> \
  --scope <global|project> \
  --trigger "<token sequence>"    Author a memory (--path for file-glob bindings)
engram forget "<name>"            Soft-demote or archive a memory
engram pin "<name>"               Pin/unpin (gate- and decay-exempt)
engram judge <id> <verdict>       Label a surfaced memory helpful|unused|noise
                                    (the evaluation watcher's verb)
engram trigger <id> --list        Show a memory's triggers (with ids)
engram trigger <id> --remove <tid> --add-path "<glob>"
                                    Narrow an over-matching trigger in place
                                    (preserves counters; add/remove/replace)
engram skip "<name>"              Mark the latest surface 'unused' (negative signal)
engram mark-noise "<name>"        Retroactively mark surfaces 'noise'
engram verify "<name>"            Mark a memory's body still accurate (staleness audit)
engram status                     Memory health JSON
engram dashboard                  HTML dashboard in browser
engram monitor                    Live watcher dashboard (active runs / 24h / decision stream)
                                    --json for a one-shot snapshot (auto when piped)
engram consolidate                Run the nightly agent now
engram seed                       Insert example memories for smoke-testing
engram rebuild-triggers           Re-extract triggers from bodies (post-migration repair)
```

## Database schema

- **memories** — content, `kind ∈ {block, hint}`, `scope ∈ {global, project}`, counters (`surface_count`, `useful_count`, `noise_count`, `last_surfaced_ts`, `pinned`, `archived_ts`, `last_verified_ts`)
- **triggers** — `kind ∈ {token_subseq, path_glob}`. `token_subseq` stores `first_token` (indexed) + `tokens_json`. `path_glob` stores an fnmatch pattern.
- **session_surfaces** — which memories surfaced when, under which hook, and the eval watcher's `outcome ∈ {helpful, unused, noise}` (NULL until judged). Per-session dedup + reinforcement targeting.
- **session_turns** — per-session tool-call counter.
- **consolidation_runs** — nightly run log with quality metrics.
- **watcher_state** — keyed `(work_session_id, role)` (formation + eval): per-role transcript cursor, resume id, and tick state (`armed`, `last_tick_ts`, `fail_streak`).

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `ENGRAM_DB` | `~/.claude/tool-engrams/db.sqlite` | SQLite DB path |
| `ENGRAM_WATCHER_MODEL` | `opus` | Model passed to `claude -p` for both watcher roles (e.g. `haiku` for a cheaper, faster watcher) |
| `ENGRAM_WATCHER_TIMEOUT` | `300` | Per-call `claude -p` timeout (seconds) for a watcher tick |
| `ENGRAM_TICK_COALESCE_SEC` | `45` | Min seconds between ticks for one (session, role); a burst of triggers coalesces into one call (flush triggers ignore it) |
| `ENGRAM_IDLE_SWEEP_SEC` | `1800` | How old a tracked session's last tick must be before the SessionStart idle-sweep treats its unread tail as abandoned and re-fires a flush tick |
| `ENGRAM_CLEANUP_TTL_SEC` | `604800` (7 days) | How cold watcher residue must be before the once-daily `engram cleanup` reaps it (dead `watcher_state` rows, stale sandbox cwds, old internal transcript dirs) |
| `ENGRAM_WATCHER_PROMPT_PATH` | unset | Override the formation prompt file |
| `ENGRAM_EVAL_PROMPT_PATH` | unset | Override the evaluation prompt file |
| `ENGRAM_CONSOLIDATION_PROMPT_PATH` | unset | Override the consolidation prompt file |

## Configurable prompts

The watcher (formation), evaluation, and consolidation agents use markdown-file prompts you can override without forking.

**Lookup order** (first match wins):

1. `$ENGRAM_<NAME>_PROMPT_PATH` — explicit file path (`<NAME>` ∈ `WATCHER`, `EVAL`, `CONSOLIDATION`)
2. `~/.claude/tool-engrams/prompts/<name>.md` — per-user override
3. Packaged defaults at `toolengrams/prompts/defaults/*.md`

Variable interpolation uses `str.format` — the formation prompt expects `{cwd}`; the consolidation prompt expects `{target_date}`, `{session_list}`, `{memory_summary}`.

## What this explicitly doesn't do

- **Semantic error detection on exit 0** (query returns empty when it shouldn't). Needs an LLM in the hot path; out of scope.
- **Conversational RAG-style memory.** Different problem; use mem0 or similar.
- **Destructive-command blocking as the pitch.** Claude Code's permission rules are the right tool for that. Blocks exist as a narrow option, not the headline.
- **Hebbian co-activation.** Removed — recall itself needs to be reliable first.
- **MCP server / non-Claude-Code harnesses.** Maybe later.

## Testing

```bash
pytest                          # Unit tests (~420, fast — no network, no LLM)
pytest tests/e2e/ -m e2e        # E2E tests (spawns real `claude -p` sessions, opt-in)
```

## Uninstall

```bash
# Remove hooks from ~/.claude/settings.json (manually or re-run install.sh flags)
rm ~/.claude/skills/engram-{remember,forget,recall}
engram consolidate --uninstall-schedule
rm -rf ~/.claude/tool-engrams/
pip uninstall toolengrams
```
