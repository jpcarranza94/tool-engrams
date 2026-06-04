# ToolEngrams

**Tool-bound memory for Claude Code.** Agent-facing tools become self-documenting through interaction: Claude fails a call, the system remembers why, and next session (or next month) arrives with that knowledge in hand.

> **Status:** alpha. Breaking changes expected; no stable users to protect. See `docs/design-v9.md` for the current design.

## The problem

Claude Code is great at well-known CLIs. It's not great at:

- **Custom CLIs** not in training data (your company's internal tools, bespoke wrappers)
- **Project-specific conventions** bound to commands ("this repo's test command needs REUSE_DB=1", "deploy requires cd into frontend/ first")
- **Subtle semantic gotchas** in databases, APIs, and frameworks (the wrong column name, the surprising flag, the workaround only the tribe knows)

For all of these, the useful information is bound to a specific tool-call pattern. Generic RAG / conversational memory doesn't help — the agent needs the fact *right when it's about to act*.

## System overview

Five components talk to Claude Code via its hook API:

```
                      Claude Code
         ┌─────────────────────────────────────┐
         │  tool call  │   hooks pipe JSON     │
         ▼             ▼                       ▼
   ┌──────────┐ ┌──────────────┐  ┌─────────────────────┐
   │ pretool  │ │  post-tool-  │  │ stop / flush /      │
   │ (block)  │ │  failure     │  │ post-tool / prompt  │
   │          │ │  (hint+arm)  │  │ (tick triggers)     │
   └────┬─────┘ └──────┬───────┘  └──────────┬──────────┘
        │              │                     │ fires detached tick
        │              │                     ▼
        │              │          ┌─────────────────────┐
        │              │          │ watcher-tick        │
        │              │          │ (claude -p --resume,│
        │              │          │ $ENGRAM_WATCHER_MODEL)
        │              │          │ per event, forms    │
        │              │          │ memories from the   │
        │              │          │ transcript delta    │
        │              │          └──────────┬──────────┘
        │              │                     │
        ▼              ▼                     ▼
   ┌────────────────────────────────────────────────────┐
   │               SQLite  (~/.claude/tool-engrams/)    │
   │  memories │ triggers │ session_surfaces │ ...      │
   └────────────────────────────────────────────────────┘
                           ▲
                           │ nightly read+prune+discover
                  ┌────────┴──────────┐
                  │ consolidation     │
                  │ (Opus, 8am daily) │
                  └───────────────────┘
```

Every component reads/writes the same SQLite DB. No network, no LLM on the hot path (hooks are single-digit-ms SQL lookups).

| Component | Code | Runs when | Role |
|---|---|---|---|
| **PreToolUse hook** | `toolengrams/hooks/pretool.py` | Before every whitelisted tool call | Looks up `block`-kind memories; denies the call + injects body on match |
| **PostToolUse hook** | `toolengrams/hooks/post_tool.py` | After tool success (exit 0 or semantically-OK non-zero) | Reinforcement bookkeeping: bump `useful_count` for memories that surfaced, increment session turn counter |
| **PostToolUseFailure hook** | `toolengrams/hooks/post_tool_failure.py` | After tool failure (exit ≠ 0 / structural error) | Looks up `hint`-kind memories; injects body as `additionalContext` (non-blocking) |
| **Watcher** | `toolengrams/watcher/tick.py` | Event-driven `claude -p` (model via `$ENGRAM_WATCHER_MODEL`, default opus); a detached tick fires per turn (Stop), recovery, correction, or session-end | Reads JSONL transcript delta, calls `engram remember` for new patterns |
| **Consolidation** | `toolengrams/consolidation/agent.py` | Nightly (launchd/cron) | Opus agent reviews yesterday's sessions — prunes noise, discovers missed patterns, deduplicates |

## Surfacing pipeline

When Claude is about to run a tool call, this is the full path from input → injected memory. The pipeline is the same in PreToolUse and PostToolUseFailure — just different `kind` filter and different output field.

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
┌─────────────────────────┐
│ 4. kind filter          │   pretool: kind='block'
│                         │   post-tool-failure: kind='hint'
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐   final_score = structural × (0.5+usefulness)
│ 5. score each candidate │                 × (0.5+0.5×recency) × (1.5 if pinned)
│    (reinforcement/      │   see "Reinforcement loop" below
│     scoring.py)         │
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
┌─────────────────────────┐   sort by (longest matching trigger DESC, score DESC)
│ 7. specificity sort     │   → more specific memories surface first
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐   log to session_surfaces, bump surface_count
│ 8. log + bump + emit    │   emit additionalContext (+ deny for blocks)
└─────────────────────────┘
```

There is **no cluster-level quality gate**. The two-kind model (blocks are user-authored and rare; hints only fire on real failures) makes per-cluster filtering redundant — quality is shaped by the reinforcement loop (below) and by the watcher / consolidation agents pruning noise out-of-band.

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
| **hint** | `PostToolUseFailure` (real failures only — exit code + Claude Code's own success/failure discrimination) | Injects body as `additionalContext` (no deny — the call already failed) | Default. Most discoveries land here. |
| **block** | `PreToolUse` (before every whitelisted call) | **Denies** the call + injects body. The deny is invisible to the user; it fails the call *for Claude* and prompts an in-loop retry with context. | Rare. Only for things you actively want to prevent (destructive ops, silently-wrong output). |

Most users will author zero `block` memories. Hints carry the weight.

## Memory formation

Memories are born three ways, in decreasing automation:

| Source | Code | Where triggers come from |
|---|---|---|
| **Watcher** (background `claude -p`) | `toolengrams/watcher.py` + `toolengrams/prompts/defaults/watcher.md` | LLM (default opus, override via `$ENGRAM_WATCHER_MODEL`) extracts a small JSON object `{name, body, kind, scope, triggers, paths}` and calls `engram remember`. The JSON schema enforces the shape. |
| **Consolidation** (Opus nightly) | `toolengrams/consolidation/agent.py` + `toolengrams/prompts/defaults/consolidation.md` | Opus issues `engram remember / forget` commands directly after reviewing yesterday's sessions. |
| **Manual** | `toolengrams/cli/remember.py` | `--trigger "<tokens>"` flag, or extracted from body via `formation/candidates.py` (backticked shell snippets, paths, URLs). |

All three paths converge at `toolengrams/formation/triggers.py` (`insert_candidate_triggers`), which writes the `first_token` + `tokens_json` rows used by the surfacing pipeline.

```bash
# Manual example — the watcher would produce the same shape automatically
engram remember \
  "Use --force-with-lease; --force overwrites co-workers' pushed commits." \
  --kind block --scope global \
  --trigger "git push --force" \
  --trigger "git push -f"
```

The watcher and consolidation prompts are **user-overridable** without forking — see "Configurable prompts" below.

## Reinforcement loop

Every memory tracks two counters that drive scoring:

| Counter | Bumped in | When |
|---|---|---|
| `surface_count` | `reinforcement/counters.py::bump_surface_counts` | A memory surfaced on this call (in either pretool or post-tool-failure). |
| `useful_count` | `reinforcement/counters.py::bump_useful_counts` | The tool call succeeded *after* this memory had surfaced on the previous call of the same tool in this session. |

`useful_count / surface_count` (Laplace-smoothed to `(useful+1)/(surface+2)`) is the memory's **usefulness** — its proven rate of helping. Recency decays calendar-time via `exp(-days / half_life)` (block: 30d, hint: 60d). Pinning multiplies the final score by 1.5.

```
usefulness = (useful_count + 1) / (surface_count + 2)          # 0.5 at cold start
recency    = exp(-days_since_last_surface / half_life_days)    # 1.0 if never surfaced
final      = structural_match × (0.5 + usefulness)
                               × (0.5 + 0.5 × recency)
                               × (1.5 if pinned)
```

The per-cluster Laplace threshold (keyed on `first_token`) filters hints whose final score is below the cluster's smoothed mean. It's a self-adjusting quality gate: a cluster with many high-scoring memories raises its own bar. Blocks bypass it — user-authored, rare, always trusted.

## Code layout

```
toolengrams/
├── hooks/             ← one file per Claude Code hook event
│   ├── pretool.py             block-kind surfacing + deny
│   ├── post_tool.py           success reinforcement
│   ├── post_tool_failure.py   hint-kind surfacing (non-blocking)
│   ├── session_start.py       session tracking + formation guidance
│   ├── user_prompt.py         fires a watcher tick on a likely correction
│   ├── stop.py                primary watcher tick trigger (turn boundary)
│   └── flush.py               final watcher tick (SessionEnd / PreCompact)
├── retrieval/         ← read path (tool call → memories)
│   ├── extract.py             tool payload → (tokens, paths)
│   ├── rank.py                candidates, subseq match, score, filter
│   └── session_state.py       session_surfaces + session_turns helpers
├── formation/         ← write path (body → memory + triggers)
│   ├── candidates.py          extract triggers from markdown body
│   ├── triggers.py            write to `triggers` table
│   ├── dedup.py               merge new memories into overlapping ones
│   └── secrets.py             reject bodies containing API keys etc.
├── reinforcement/     ← scoring + counter bumps
│   ├── scoring.py             usefulness, recency, final_score
│   └── counters.py            surface_count / useful_count bumps, archive
├── consolidation/     ← nightly Opus agent
├── prompts/
│   ├── defaults/              shipped markdown prompts (watcher, consolidation)
│   ├── loader.py              env-var / user-override / default lookup chain
│   ├── watcher.py             thin loader for watcher.md
│   └── consolidation.py       thin loader for consolidation.md
├── cli/               ← user-facing subcommands (remember, recall, pin, …)
├── migrations/        ← SQL schema evolution (v*.sql)
├── schema.sql         ← complete v_latest snapshot for fresh DBs
├── db.py              ← connection + migration runner
├── models.py          ← dataclasses (Memory, Trigger, Candidate, …)
└── watcher/           ← event-driven formation (tick.py core; agent/lifecycle/transcript_format)
```

The hot-path (hooks) has **no external dependencies** — stdlib + sqlite3 only. LLMs run only in the watcher tick (`watcher/tick.py`, model via `$ENGRAM_WATCHER_MODEL`, default opus) and `consolidation/agent.py` (Opus), both out-of-band from the tool-call path.

## Install

```bash
git clone https://github.com/jpcarranza94/tool-engrams.git
cd tool-engrams
./install.sh
```

The installer:

1. Installs `toolengrams` (pip editable)
2. Wires hooks into `~/.claude/settings.json`:
   - `SessionStart`, `UserPromptSubmit` (watcher lifecycle)
   - `PreToolUse` (block surfacing)
   - `PostToolUse` (reinforcement)
   - `PostToolUseFailure` (hint surfacing)
3. Symlinks skills (`/engram-remember`, `/engram-forget`, `/engram-recall`)
4. Initializes the SQLite DB at `~/.claude/tool-engrams/db.sqlite`
5. Optionally schedules the nightly consolidation agent

### Requirements

- Python 3.10+ (stdlib + sqlite3, no deps on the hot path)
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
engram pin "<name>"               Pin/unpin (ignored by reinforcement decay)
engram status                     Memory health JSON
engram dashboard                  HTML dashboard in browser
engram monitor                    Watcher process health + recent activity
engram consolidate                Run the nightly agent now
engram seed                       Insert example memories for smoke-testing
engram migrate-v1-to-v2           One-shot migration for pre-v2 installs
engram rebuild-triggers           Re-extract triggers from bodies (post-migration repair)
```

## Database schema

- **memories** — content, `kind ∈ {block, hint}`, `scope ∈ {global, project}`, reinforcement counters (`surface_count`, `useful_count`, `last_surfaced_ts`, `pinned`, `archived_ts`)
- **triggers** — `kind ∈ {token_subseq, path_glob}`. `token_subseq` stores `first_token` (indexed) + `tokens_json`. `path_glob` stores an fnmatch pattern.
- **session_surfaces** — which memories surfaced when, under which hook. Per-session dedup + reinforcement targeting.
- **session_turns** — per-session tool-call counter.
- **consolidation_runs** — nightly run log with quality metrics.
- **watcher_state** — per-session transcript cursor + event-driven tick state (`armed`, `last_tick_ts`, `fail_streak`).

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `ENGRAM_DB` | `~/.claude/tool-engrams/db.sqlite` | SQLite DB path |
| `ENGRAM_WATCHER_MODEL` | `opus` | Model passed to `claude -p` for the watcher (e.g. `haiku` for ~20× cost reduction at the cost of more parse errors) |
| `ENGRAM_WATCHER_TIMEOUT` | `120` | Per-call `claude -p` timeout (seconds) for a watcher tick |
| `ENGRAM_TICK_COALESCE_SEC` | `45` | Min seconds between watcher ticks for one session; a burst of triggers coalesces into one call (flush triggers ignore it) |
| `ENGRAM_WATCHER_PROMPT_PATH` | unset | Override the watcher's prompt file (see below) |
| `ENGRAM_CONSOLIDATION_PROMPT_PATH` | unset | Override the consolidation agent's prompt file |

## Configurable prompts

The watcher and consolidation agents use markdown-file prompts you can override without forking.

**Lookup order** (first match wins):

1. `$ENGRAM_WATCHER_PROMPT_PATH` or `$ENGRAM_CONSOLIDATION_PROMPT_PATH` — explicit file path
2. `~/.claude/tool-engrams/prompts/watcher.md` or `~/.claude/tool-engrams/prompts/consolidation.md` — per-user override
3. Packaged defaults at `toolengrams/prompts/defaults/*.md`

Variable interpolation uses `str.format` — the consolidation prompt expects `{target_date}`, `{session_list}`, `{memory_summary}`.

## What this explicitly doesn't do

- **Semantic error detection on exit 0** (query returns empty when it shouldn't). Needs LLM in the hot path; out of scope.
- **Conversational RAG-style memory.** Different problem; use mem0 or similar.
- **Destructive-command blocking as the pitch.** Claude Code's permission rules are the right tool for that. Blocks exist as a narrow option, not the headline.
- **Hebbian co-activation.** Removed — recall itself needs to be reliable first before a secondary ranking signal is worth maintaining.
- **MCP server / non-Claude-Code harnesses.** Maybe later.

## Testing

```bash
pytest                          # Unit tests (~200, fast — no network, no LLM)
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
