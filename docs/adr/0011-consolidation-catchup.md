# ADR-0011 — Consolidation is a gap-driven catch-up sweep, not "yesterday from now"

- **Status:** Accepted
- **Context for:** intermittent-machine reality — the laptop is often off when
  the 8 AM job would fire

## Context

The scheduled run consolidated exactly one day: `date.today() - 1`, computed at
execution time (`cli/consolidate.py::_resolve_date`). A day's sessions are only
ever picked up by the run that fires the very next day — `collect_sessions`
matches transcript files by `mtime == target_date`.

Two failure modes compounded:

1. **Time-relative date.** If the machine is off across a whole calendar day,
   that day's run never fires, and the run that *does* fire next computes a
   different "yesterday." A session-heavy Friday followed by a laptop-off
   Saturday is never revisited: Sunday's boot run consolidates Saturday (empty)
   and Friday is orphaned forever.
2. **launchd coalesces missed `StartCalendarInterval` jobs** into a single
   wake-time run, and `RunAtLoad` was `false`, so a multi-day absence produced
   one run for the most recent "yesterday" — not one per missed day.

Observed in the wild: 2026-06-14 (Sat, laptop off) → `no_sessions`, while the
2026-06-12/13 runs had separately died on `Failed to spawn agent` and were
recorded anyway, permanently skipping days that genuinely had sessions.

## Decision

`--yesterday` (the scheduled flag) becomes a **catch-up sweep** driven by the
`consolidation_runs` table as the source of truth for coverage:

1. Candidate dates = `[today - CATCHUP_LOOKBACK_DAYS … yesterday]`,
   oldest-first (a later day's surfacing evaluation should see the memory state
   earlier days left behind).
2. Per day: skip if `runs.was_run()` (idempotent; `--force` bypasses); skip if
   `collect_sessions()` is empty; else consolidate.
3. **Empty days are not recorded.** They cost only a cheap disk glob per scan,
   bounded by the window — recording them would pollute run history and the
   dashboard's "last run."
4. **Errored days are not recorded either** — a transient failure
   (spawn/timeout/PATH) leaves the day un-run so the next sweep retries it,
   within the window. Previously an error still wrote a row and the day was lost.
5. `CATCHUP_LOOKBACK_DAYS = 7`. No per-run spawn cap: the window already bounds
   the worst case to 7 agent calls on a first boot after a week away.

`RunAtLoad` flips to `true` (macOS). The sweep is idempotent across *sequential*
fires (`was_run` skips done days), so firing it on every login/boot drains
backlog promptly instead of waiting for the next 8 AM the machine might also
miss. But `RunAtLoad` + the 8 AM `StartCalendarInterval` makes *concurrent*
fires reachable (boot near 8 AM; or a multi-day sweep still running — minutes
per day — when the calendar job lands), and `was_run` is not concurrency-safe:
a ~30-min agent call sits between the `was_run` check and `record_run`, so two
sweeps could both spawn an Opus agent for the same day. A **non-blocking
`flock`** (`consolidate.lock` next to the DB) wraps the mutating path: a second
overlapping sweep exits cleanly (`{"action":"skipped","reason":"already_running"}`)
rather than double-spending. `PRAGMA busy_timeout` makes a contended writer wait
rather than fail fast in the window before the flock catches.

`--date D` stays a single explicit day (manual backfill); no flag stays "today."

## Alternatives considered

- **Record empty/errored days as rows** (idempotency via a marker): stops the
  cheap rescan, but pollutes `last_run`/`recent_runs` with zero-rows and — for
  errors — re-introduces the permanent-skip bug. The 7-day window makes the
  rescan negligible, so the marker buys nothing.
- **Rely on launchd missed-run semantics** (just set `RunAtLoad`): still
  single-day and still coalesces; a full-day-off gap is unrecoverable. The bug
  is the time-relative date, not only the scheduler.
- **Drive candidate dates from `session_turns`/`session_surfaces`** (sqlite
  activity) instead of a fixed window of disk globs: the transcript files are
  the content the agent actually reads, and the glob is cheap; the DB activity
  tables would be a second, drift-prone source of "which days had work."
- **Per-run spawn cap (e.g. 3/run):** unnecessary once the lookback bounds the
  window to 7; a cap only delays a backlog the window already limits.

## Consequences

- A day missed because the laptop was off is backfilled on the next run within
  7 days; older gaps are dropped (and only matter if transcripts that old still
  exist under `~/.claude/projects`).
- Transient agent failures self-heal across runs instead of silently burning a
  day. A *persistently* failing day retries every run until it ages out of the
  window — acceptable, and visible in `consolidate.err`.
- One invocation can now spawn up to 7 consolidation agents (first boot after a
  week away). Each is still gated by `was_run`, so steady-state boots spawn zero.
- **Concurrency is bounded by the `flock`, not eliminated by `was_run`.** Two
  sweeps can still both pass `was_run` for a day if neither has recorded it yet,
  but only one holds the lock, so only one runs — the loser exits without
  spending. The lock is the load-bearing guard; `was_run` is the cheap skip.
- **Linux is only half-covered.** `RunAtLoad` is a launchd concept; the Linux
  cron line stays `0 8 * * *` with no `@reboot`, so a Linux box off at 8 AM
  relies on the 7-day window catching up on the next 8 AM it *is* on — it does
  not get the boot-time drain. Acceptable while the only user is on macOS;
  revisit (add an `@reboot` cron line) if a Linux host needs the same guarantee.
- `--yesterday --json` output changed shape: a top-level
  `{status, surfaces_cleaned, runs: [...]}` aggregate instead of one per-day
  object. The launchd/cron args are unchanged, so no schedule reinstall is
  needed for the logic — but `RunAtLoad` requires a reinstall
  (`engram consolidate --install-schedule`) to take effect.
