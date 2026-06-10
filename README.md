# ToolEngrams

**Tool-bound memory for Claude Code.** Agent-facing tools become self-documenting through interaction: Claude fails a call, the system remembers why, and next session (or next month) arrives with that knowledge in hand.

> **Status:** alpha. Breaking changes expected; no stable users to protect. See `docs/design.md` for the design (and `docs/adr/` for the load-bearing decisions).

> **Off switch:** `engram pause` stops everything with one command — no surfacing, no background watchers, no spend. `engram resume` turns it back on (`ENGRAM_DISABLED=1` does the same per shell). This system runs background LLM sessions that cost real money — read [Cost, privacy & the off switch](#cost-privacy--the-off-switch) before installing.

## Quickstart

```bash
git clone https://github.com/jpcarranza94/tool-engrams.git
cd tool-engrams
./install.sh        # wires hooks, initializes the DB, verifies with `engram doctor`
engram seed         # plants demo memories for the smoke test
```

Then **open a new Claude Code session** — hooks load at session start, so an
already-running session won't have them — and walk through
[Verify it's working](#verify-its-working).

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
trigger:   ["mycli", "order", "reassign"]
matches:   mycli order 12345 reassign
matches:   mycli --env staging order abc reassign --reason X
no match:  mycli reassign order    (wrong order)
no match:  mycli customer reassign (missing "order")
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
| **Formation watcher** (background `claude -p`) | `toolengrams/watcher/` + `toolengrams/prompts/defaults/watcher.md` | A permissioned session (model via `$ENGRAM_WATCHER_MODEL`, default sonnet) whose only allowed command is `engram remember`. It reads the transcript delta and runs `engram remember …` for patterns worth keeping — native tool-calling, no JSON schema. |
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

## Cost, privacy & the off switch

**What runs in the background.** After a turn completes (Stop), the hooks fire detached `claude -p` sessions: one **formation** tick (forms memories from the transcript delta) and, only when surfaced memories are pending judgment, one **evaluation** tick. Rapid turns coalesce (default 45s window), so it's at most one model call per role per coalesced Stop event — not per tool call. A nightly **consolidation** agent (Opus) reviews the day's sessions. Nothing model-driven runs on the tool-call hot path.

**What it costs.** Preliminary figure from one day of mixed opus/sonnet data: **~$1/day** at moderate use with the default `sonnet` watcher. Evaluation calls cost roughly 5× formation calls (they re-read surfaced context). Treat this as an estimate, not a measured fact, until more sonnet-only data is in.

**The levers.**

- `engram pause` / `engram resume` — the kill switch. Paused, every hook, watcher tick, and the nightly consolidation stand down: no surfacing, no ticks, no spend. `ENGRAM_DISABLED=1` does the same via env (it beats the flag file, so `ENGRAM_DISABLED=0` force-enables for scripts). One caveat: pausing freezes the watcher's transcript cursor rather than discarding it — after `engram resume`, the next tick reads the delta that accumulated during the pause. If you paused to keep a sensitive session out of the store, keep it paused until that session is over, or audit with `engram recall` after resuming.
- `ENGRAM_WATCHER_MODEL=haiku` — cheaper watcher; `ENGRAM_FORMATION_MODEL` / `ENGRAM_EVAL_MODEL` override per role.
- `engram monitor` — live dashboard of watcher runs with per-run USD cost and the decision stream.

**What is stored where.** Everything is local; nothing leaves your machine except the `claude -p` calls themselves (same data path as your normal Claude Code usage).

- Memory bodies, triggers, and surface history: SQLite at `~/.claude/tool-engrams/db.sqlite`.
- Transcript deltas the watcher reads: written to per-(session, role) sandbox working dirs; transcript excerpts may appear in watcher decision logs.
- Watcher residue (sandbox cwds, internal transcripts, dead state rows) is reaped by a once-daily cleanup after 7 days (`ENGRAM_CLEANUP_TTL_SEC`).

**Secrets gate.** `engram remember` rejects bodies that look like credentials (API keys, tokens, passwords, connection strings), so the formation watcher can't store them as memories.

## Security

ToolEngrams injects stored text into Claude's context and can deny tool calls — treat its data as part of your attack surface:

- **Memory bodies are untrusted input to future sessions.** They form autonomously from transcripts via a background LLM, and they're injected at PreToolUse — a poisoned or prompt-injecting memory body would speak with the system's voice. `block`-kind memories deserve the most scrutiny: their body is delivered alongside a denied tool call, the moment Claude is most likely to comply. Audit what's stored with `engram recall` and watch formation decisions in `engram monitor`.
- **The watcher's containment boundary is a command allowlist, not trust in the model.** Each watcher role runs `claude -p` with a per-role allowlist — the formation session may only run `engram remember`, the evaluation session only `engram judge`. A hijacked watcher can write bad memories (auditable, reversible) but can't run arbitrary commands.
- **The secrets gate** (above) keeps credentials out of the store, so a leaked DB or surfaced memory can't replay them.
- **The kill switch** (`engram pause`) takes the whole system offline in one command while you investigate anything suspicious.

## Install

```bash
git clone https://github.com/jpcarranza94/tool-engrams.git
cd tool-engrams
./install.sh
```

(Uninstall any time with `./install.sh --uninstall` — it removes the hooks and
skills but keeps your memories.)

The installer:

1. Installs `toolengrams` (pip editable)
2. Wires hooks into `~/.claude/settings.json`:
   - `SessionStart` (session tracking + idle-sweep), `UserPromptSubmit` (formation tick on correction)
   - `Stop` (formation + eval tick triggers), `SessionEnd`/`PreCompact` (flush ticks)
   - `PreToolUse` (block + hint surfacing)
   - `PostToolUse` (turn counter + recovery tick)
   - `PostToolUseFailure` (hint surfacing + arms the watcher)
3. Symlinks skills (`/engram-remember`, `/engram-forget`, `/engram-recall`)
4. Initializes the SQLite DB at `~/.claude/tool-engrams/db.sqlite` and verifies
   the whole wiring with `engram doctor`
5. Optionally schedules the nightly consolidation agent (skipped automatically
   on non-interactive installs)

### Verify it's working

Hooks load at session start — **open a new Claude Code session** after
installing. The session you ran `./install.sh` from won't have them.

```bash
engram seed                        # 1. plant three demo hint memories
export ENGRAM_SURFACE_NOTICE=1     # 2. optional: print a visible line when a memory fires
```

(Hooks inherit the `claude` process environment — export
`ENGRAM_SURFACE_NOTICE` in the same shell you launch the new session from,
or it silently does nothing.)

3. In the new session, ask Claude to run `ssh deploy@production`. The seeded
   VPN hint is injected alongside the call — ask Claude what context it
   received, or watch for the `ToolEngrams surfaced: …` line if you enabled
   the notice.
4. `engram status` — the surfaces count incremented.
5. `engram doctor` — wiring + liveness diagnostics (hooks present, `engram` on
   PATH, claude version, DB schema, when a hook last fired). Exit code 1 on
   any failure, so it's scriptable.
6. `engram seed --remove` — delete the demo memories again.

Want to see the deny path? `engram seed --with-block` adds one `block` memory
on `git push --force` (a call you won't trip by accident): Claude's forced
push is denied and redirected to `--force-with-lease`.

**Expect the first day to be quiet.** Organic memories form from real
failure→recovery episodes, not from every turn. Watch formation decisions
live with `engram monitor`.

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
engram pause                      Kill switch: stop surfacing, ticks, and spend
engram resume                     Turn the system back on
engram status                     Memory health (human on a tty; JSON when piped
                                    or with --json — pipe-safe for scripts)
engram doctor                     Wiring + liveness diagnostics: hooks present,
                                    PATH, claude version, DB, last hook fire.
                                    Exit 1 on failure (--json available)
engram dashboard                  HTML dashboard in browser
engram monitor                    Live watcher dashboard (active runs / 24h / decision stream)
                                    --json for a one-shot snapshot (auto when piped)
engram consolidate                Run the nightly agent now
engram seed [--with-block]        Insert example memories for smoke-testing
engram seed --remove              Delete the seeded examples (exact names only)
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
| `ENGRAM_DISABLED` | unset | `1`/`true` disables the whole system (beats the `engram pause` flag file; `0`/`false` force-enables) |
| `ENGRAM_SURFACE_NOTICE` | unset | `1`/`true` adds a visible `ToolEngrams surfaced: …` line to the transcript whenever a memory is injected — for the post-install smoke test and surfacing debugging |
| `ENGRAM_WATCHER_MODEL` | `sonnet` | Model passed to `claude -p` for both watcher roles (e.g. `haiku` for a cheaper, faster watcher) |
| `ENGRAM_FORMATION_MODEL` | unset | Model for the formation role only; beats `ENGRAM_WATCHER_MODEL` |
| `ENGRAM_EVAL_MODEL` | unset | Model for the evaluation role only; beats `ENGRAM_WATCHER_MODEL` |
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
pytest                          # Unit tests (fast — no network, no LLM)
pytest tests/e2e/ -m e2e        # E2E tests (spawns real `claude -p` sessions, opt-in)
```

## Uninstall

```bash
./install.sh --uninstall              # removes hooks, permission, skill symlinks
rm -rf ~/.claude/tool-engrams/        # only if you also want the memories gone
pip uninstall toolengrams             # venv-fallback installs instead:
                                      #   rm -rf ~/.local/share/toolengrams/venv ~/.local/bin/engram
```
