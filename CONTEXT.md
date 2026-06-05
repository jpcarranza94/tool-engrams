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
_Avoid_: feedback/reference (the retired v1 kinds), guard, tip, warning.

**Reinforcement**:
The counting that scores a memory — `useful_count / surface_count`, bumped from the
post-tool hooks. Distinct from formation (creating memories) and consolidation.
_Avoid_: scoring (too generic), feedback, learning.

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

**Consolidation**:
The nightly Opus agent that reviews the day's sessions to prune noise, dedupe, and
discover missed patterns. Separate from the watcher and from reinforcement.
_Avoid_: cleanup, compaction, the nightly job.
