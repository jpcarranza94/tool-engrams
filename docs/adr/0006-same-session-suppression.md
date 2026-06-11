# ADR-0006 — A memory never surfaces into the session that formed it (hints)

- **Status:** Accepted

## Context

The formation watcher saves memories from a session's transcript while that
session is still running. Nothing stopped a just-formed memory from surfacing
right back into its origin session on the next matching tool call. That
surface is wrong three ways:

1. **Redundant.** The episode that formed the memory is already in the main
   session's own context — the model just lived it. (Observed live: a
   "this repo now has CI" hint saved mid-session surfaced back at its own
   author two hours later.)
2. **It corrupts the reinforcement signal.** `q` is supposed to measure
   cross-session transfer — did stored knowledge help a *future* session? A
   memory surfacing into its origin session gets judged `helpful` almost
   automatically: the model follows it because it learned the same lesson
   independently, minutes earlier, from the same events. That inflates
   `useful_count` with self-confirmation that proves nothing about transfer.
3. **It feeds an echo loop.** The injected body lands in the work transcript;
   the next formation delta contains the memory's own text, which the watcher
   can mistake for fresh session evidence. The watcher-child recursion guard
   covers the watcher's own session, not this transcript-level echo.

## Decision

**A `hint` whose `origin_session_id` equals the current session is never
surfaced** — in either surfacing path (PreToolUse and PostToolUseFailure).

- `memories.origin_session_id` (nullable) records the work session that
  formed the memory. The watcher's `claude -p` subprocess carries
  `ENGRAM_ORIGIN_SESSION` in its environment and `engram remember` reads it
  (env fallback to an explicit `--origin-session` flag) — attribution does
  not depend on the model remembering to pass a flag.
- **Manual saves keep `origin_session_id = NULL`** (a hand-run `engram
  remember` has no session attribution) and are therefore never suppressed.
- **A body-replacing dedup update re-stamps the origin** to the updating
  session (or NULL for a manual update): the new body belongs to whoever
  wrote it, and that session's echo is now the one to suppress.
- **`block` memories are exempt.** The session where the user just got burned
  and a block was formed is exactly the session where the deny must enforce;
  in-context knowledge is not enforcement. This mirrors the existing
  "blocks are exempt from the surfacing gate" rule.
- **Forward-only.** Pre-existing rows have no origin recorded (the column
  didn't exist), so retroactive cleanup of historically inflated counters is
  not possible — and not needed: `q` is a running ratio that dilutes under
  new, clean judgments, and consolidation prunes what doesn't hold up.

## Consequences

**Positive**
- `q` means what the design says it means: cross-session usefulness.
- Fewer junk surfaces → fewer pending rows → fewer eval ticks → less spend.
- The formation echo loop is severed at its source.

**Negative / risks**
- A very long session that compacts may genuinely lose the episode from
  context, and a same-session surface could have helped after compaction.
  Accepted: compaction summaries usually retain the lesson, `/clear` issues a
  new session id (ending suppression naturally), and the cost is one session
  of latency.
- One extra field on the surfacing hot path. Negligible: the filter is a
  string comparison on already-retrieved candidates; no extra query.
