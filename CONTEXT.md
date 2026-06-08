# ToolEngrams

A tool-bound memory system for Claude Code: memories bind to command patterns and
surface automatically through Claude Code hooks. This file fixes the domain
vocabulary so code, docs, and reviews name the same concept the same way.

## Language

### Memory model

**Memory**:
A stored piece of tool-bound knowledge (the project's name for an *engram*). Has a
body, a kind, a scope, and one or more triggers.
_Avoid_: note, record, entry, fact.

**Trigger**:
What binds a memory to a tool call — a required-token subsequence (tokens must
appear in order, gaps allowed) or a path glob.
_Avoid_: pattern, matcher, rule, key.

**Surface** (verb):
To inject a memory's body into the agent at a hook moment. A surface is counted
even when the memory turns out unhelpful.
_Avoid_: show, fire, emit, recall (as a verb).

**Block / Hint** (a memory's *kind*):
`block` denies a call at PreToolUse and injects its body; `hint` injects on tool
failure (PostToolUseFailure). These are the only two kinds.
_Avoid_: feedback, reference, guard, tip, warning.

**Reinforcement**:
The counting that scores a memory. A memory accrues `useful_count` (helpful
verdicts) and `noise_count` (noise verdicts) from the **evaluation watcher**, never
from tool-call success. Distinct from formation (creating memories) and consolidation.
_Avoid_: scoring (too generic), feedback, learning.

**Judge** (verb):
To label how a surfaced memory fared on the call it surfaced on — `helpful` (the model
followed it), `unused` (relevant, not acted on), or `noise` (the trigger over-matched).
Done by the evaluation watcher via `engram judge <memory_id> <outcome>`, reading the
model's actions *after* the surface. Distinct from _surface_ (showing a memory).
_Avoid_: score, grade, rate, evaluate (the watcher's name, not the act).

**q** (noise-aware usefulness):
The single Laplace-smoothed quality ratio `(useful_count + 1) / (useful_count +
noise_count + 2)` that drives both ranking and the surfacing gate. `unused` enters
neither counter, so a correct-but-situational memory is not punished for not being
acted on. A fresh memory sits at `q = 0.5`.
_Avoid_: usefulness (too generic), score, confidence.

**Surfacing gate**:
The PreToolUse suppression rule: a `hint` with `q < 0.5` (after a warm-up of
`useful_count + noise_count ≥ N`) is not surfaced — it has proven more noise than
signal. `block` and `pinned` memories are exempt. Distinct from the sort+cap, which
only orders what does surface.
_Avoid_: filter, threshold (alone), cutoff.

**Memory store**:
The single persistence seam for the Memory aggregate (`memories` + `triggers` +
`memories_fts`). Every SQL statement against those tables lives there; reads return
`Memory` / `Trigger` objects. Sibling seams, one per aggregate: the session store
(`session_state`), the consolidation-runs store (`consolidation/runs`), and the
watcher store (`watcher/state`).
_Avoid_: DAO, repository, query layer.

### Watcher

**Watcher**:
The event-driven background formation path. Hooks fire a detached `engram
watcher-tick` per meaningful event; there is no long-running watcher process.
_Avoid_: daemon, poller, cron, background job.

**Tick**:
One watcher event-processing cycle: read the transcript delta since the cursor →
gate → call `claude -p` → save → advance the cursor. The unit of watcher work.
_Avoid_: run, cycle, pass, poll.

**Tick state**:
The per-session state a tick reads and commits — cursor (`last_line_read`),
resume id, `armed`, `fail_streak`, `last_tick_ts`. Lives in the `watcher_state`
table, reachable only through `watcher/state.py` (the `TickState` dataclass).
_Avoid_: watcher record, session row, progress.

**Armed**:
A session flag set when a tool fails, forcing the next turn-boundary tick to call
the model even if that turn shows no tool activity — so an error→fix episode is
never gated out.
_Avoid_: pending, dirty, flagged.

**Coalesce**:
The debounce that folds a burst of tick triggers for one session into a single
model call. A debounce, not a poll: no events → no tick.
_Avoid_: throttle, rate-limit, batch.

**Idle-sweep** (tail recovery):
The SessionStart backstop that re-fires a flush tick for any abandoned session — one
with unread transcript lines and an old last tick — that died before its final
Stop/flush. Recovers a tail the coalesce window or a missing SessionEnd would
otherwise lose.
_Avoid_: cleanup, reaper, GC.

**Watcher session**:
Any of the three LLM jobs run as a permissioned `claude -p` that does its work by
calling `engram` CLI commands (no constrained JSON): **formation** (`engram remember`),
**evaluation** (`engram judge`), **consolidation** (full `engram *`). They differ only
in command surface, prompt, cadence, and cursor.
_Avoid_: agent (too generic), bot, worker.

**Evaluation watcher**:
The watcher session that judges surfaced memories. Fires at the Stop after a surface
(when surfaces are pending), reads the transcript *forward* through the model's
response, and calls `engram judge`. Has its own trailing cursor (`role='eval'` in
`watcher_state`) and its own resumed `claude` session. Distinct from formation (which
*creates* memories) and consolidation (the nightly aggregate).
_Avoid_: evaluator, grader, scorer, the eval job.

**Consolidation**:
The nightly Opus agent that reviews the day's sessions to prune noise, dedupe, and
discover missed patterns. It aggregates per-memory `helpful`/`unused`/`noise`
distributions and prefers **narrowing a trigger** over archiving when a memory is
noisy (the noise is the trigger's fault, not the content's). Separate from the watcher
ticks and from reinforcement.
_Avoid_: cleanup, compaction, the nightly job.
