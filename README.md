# ToolEngrams

**Tool-bound memory for Claude Code.** Agent-facing tools become self-documenting through interaction: Claude fails a call, the system remembers why, and next session (or next month) arrives with that knowledge in hand.

> **Status:** alpha. Breaking changes between versions. No stable users to protect. See `docs/design-v9.md` for the current design.

## The problem

Claude Code is great at well-known CLIs. It's not great at:

- **Custom CLIs** not in training data (your company's internal tools, bespoke wrappers)
- **Project-specific conventions** bound to commands ("this repo's test command needs REUSE_DB=1", "deploy requires cd into frontend/ first")
- **Subtle semantic gotchas** in databases, APIs, and frameworks (the wrong column name, the surprising flag, the workaround only the tribe knows)

For all of these, the useful information is bound to a specific tool-call pattern. Generic RAG / conversational memory doesn't help — the agent needs the fact *right when it's about to act*.

## How it works

Two hooks, one trigger mechanism.

### The canonical flow (a `hint` memory)

1. Claude runs `ergdb -c "SELECT name FROM core_statustype"` and it fails (`column "name" does not exist`).
2. Claude Code fires **PostToolUseFailure** → ToolEngrams looks up memories bound to `ergdb -c` → injects the stored correction as `additionalContext`.
3. Claude reads "the column is `label`, not `name`", retries, succeeds.
4. Next session, same failure pattern, same correction surfaces instantly — no rediscovery.

Memories bound this way are **kind: hint**. They surface *only* on real tool failures (Claude Code's `PostToolUseFailure` event, which already discriminates structural failures from semantic non-zero exits like `grep` no-match). No noise on every call.

### The rare flow (a `block` memory)

For the narrow class where a failure mode is expensive or invisible — destructive ops, silently-wrong outputs — you can author a **kind: block** memory. It fires on **PreToolUse**, Claude's call gets denied, the stored context lands in Claude's next turn, Claude retries with the correction. The user never sees the deny; it's an in-loop correction.

```bash
engram remember "Use --force-with-lease; --force overwrites co-workers' pushed commits." \
  --kind block --scope global \
  --trigger "git push --force" \
  --trigger "git push -f"
```

Expect most users to author zero of these. Hints are the default.

### Triggers: subsequence match on tokens

A trigger is a list of required tokens in order. The tool call matches if all trigger tokens appear in the tokenized call, in the same order — **gaps allowed**.

```
trigger:   ["ergeon", "order", "reassign"]
matches:   ergeon order 12345 reassign
matches:   ergeon --env staging order abc reassign --reason X
no match:  ergeon reassign order    (wrong order)
no match:  ergeon customer reassign (missing "order")
```

This handles the positional-ID-between-verbs case that simple prefix matching can't (`gh pr 123 comment`, `kubectl get pod-abc123 describe`, `jira sprint 5 add`).

### Memory formation

Memories are created three ways:

| Layer | Model | When | Job |
|---|---|---|---|
| **Watcher** | Haiku (background) | Every 5 min | Reviews the conversation delta; catches failed calls + corrections Claude made in the moment |
| **Consolidation** | Opus (nightly) | Daily | Reviews yesterday's full sessions; prunes noise, discovers patterns the watcher missed, dedupes |
| **Manual** | — | User or Claude initiated | `engram remember` |

The watcher and consolidation prompts are user-overridable — see "Configurable prompts" below.

## Install

```bash
git clone https://github.com/jpcarranza94/tool-engrams.git
cd tool-engrams
./install.sh
```

The installer:

1. Installs `toolengrams` (pip editable)
2. Adds the two hooks (`PreToolUse`, `PostToolUseFailure`) + watcher spawn (`SessionStart`, `UserPromptSubmit`) to `~/.claude/settings.json`
3. Symlinks skills (`/engram-remember`, `/engram-forget`, `/engram-recall`) into `~/.claude/skills/`
4. Initializes the SQLite DB at `~/.claude/tool-engrams/db.sqlite`
5. Optionally schedules the nightly consolidation agent

### Requirements

- Python 3.10+ (stdlib + sqlite3, no deps on the hot path)
- Claude Code ≥ 2.1.117 (needs the `PostToolUseFailure` hook event)

## CLI

```
engram recall [query]             # Browse and search memories
engram recall --id N              # Full detail on one memory
engram recall --stats             # Summary counts by kind/scope
engram remember "<body>" \
  --kind <block|hint> \
  --scope <global|project> \
  --trigger "<token sequence>"    # Author a memory (--path for file-glob bindings)
engram forget "<name>"            # Soft-demote or archive a memory
engram pin "<name>"               # Pin/unpin a memory (ignored by reinforcement decay)
engram status                     # Memory health JSON
engram dashboard                  # Open HTML dashboard in browser
engram monitor                    # Watcher process health + recent activity
engram consolidate                # Run the nightly agent now
engram seed                       # Insert example memories for smoke-testing
engram migrate-v1-to-v2           # One-shot DB migration for pre-v2 installs
```

## Architecture

```
~/.claude/tool-engrams/
  db.sqlite                       SQLite (memories, triggers, session state)
  watcher.log                     Watcher activity
  consolidate.log                 Consolidation output
  prompts/                        Optional per-user prompt overrides (see below)

~/.claude/settings.json           Hook config (written by install.sh)
~/.claude/skills/                 Skill symlinks
```

### DB shape (v2)

- **memories** — content, `kind ∈ {block, hint}`, `scope ∈ {global, project}`, reinforcement counters (`surface_count`, `useful_count`, `last_surfaced_ts`, `pinned`, `archived_ts`)
- **triggers** — `kind ∈ {token_subseq, path_glob}`. `token_subseq` stores `first_token` (indexed) + `tokens_json`. `path_glob` stores an fnmatch pattern.
- **session_surfaces** — which memories surfaced when, under which hook. Per-session dedup + reinforcement targeting.
- **session_turns** — per-session tool-call counter.
- **consolidation_runs** — nightly run log with quality metrics.
- **watcher_state** — active watcher processes (PID, transcript cursor).

### Scoring

```
usefulness = (useful_count + 1) / (surface_count + 2)   # Laplace-smoothed
recency    = exp(-days_since_last_surface / half_life)   # block: 30d, hint: 60d
final      = structural_match × (0.5 + usefulness) × (0.5 + 0.5 × recency)
             × (1.5 if pinned)
```

Blocks skip the per-cluster Laplace quality gate (they're rare and user-authored — always surface). Hints share a cluster threshold to filter noise.

## Configurable prompts

The watcher and consolidation agents use markdown-file prompts you can override without forking.

**Lookup order** (first match wins):

1. `$ENGRAM_WATCHER_PROMPT_PATH` or `$ENGRAM_CONSOLIDATION_PROMPT_PATH` — explicit file path
2. `~/.claude/tool-engrams/prompts/watcher.md` or `~/.claude/tool-engrams/prompts/consolidation.md` — per-user override
3. Packaged defaults at `toolengrams/prompts/defaults/*.md`

Variable interpolation uses `str.format` — the consolidation prompt expects `{target_date}`, `{session_list}`, `{memory_summary}`.

## What v2 explicitly doesn't do

- **Semantic error detection on exit 0** (query returns empty when it shouldn't). Needs LLM in the hot path; out of scope.
- **Conversational RAG-style memory.** Different problem; use mem0 or similar.
- **Destructive-command blocking as the pitch.** Claude Code's permission rules are the right tool for that. Blocks exist as a narrow option, not the headline.
- **Hebbian co-activation.** Removed in v2 — recall itself needs to be reliable first before a secondary ranking signal is worth maintaining.
- **MCP server / non-Claude-Code harnesses.** Maybe later.

## Testing

```bash
pytest                          # Unit tests (fast, ~200 tests)
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
