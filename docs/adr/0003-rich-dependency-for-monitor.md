# ADR-0003 — The live monitor takes `rich` as a hard dependency

- **Status:** Accepted
- **Context doc:** `docs/design.md` §8

## Context

`engram monitor` is a live, auto-refreshing terminal dashboard (active runs, last 24h,
the decision stream). The rest of the package has **zero external dependencies** — stdlib
+ sqlite3 only — which is a genuine selling point ("install it, nothing else comes along")
and a hard requirement on the hot path (PreToolUse runs on every tool call and must stay
single-digit-ms).

A live multi-pane dashboard can be built three ways: a stdlib ANSI redraw loop (zero deps,
but hand-rolled layout/flicker management), stdlib `curses` (zero deps, fiddlier, weaker on
Windows), or a library (`rich`/`textual`). `rich` gives the best polish for the least
effort and has a negligible dependency footprint of its own.

## Decision

**`engram monitor` uses `rich` as a hard runtime dependency**, with two guardrails:

1. **Confined to the renderer.** `rich` is imported only in `cli/monitor.py`. Hooks,
   `retrieval`, and `watcher/tick.py` import nothing but stdlib + sqlite3, so the hot path
   is untouched.
2. **The data layer is rich-free.** The dashboard's pane builders are pure functions over
   the `watcher_runs` / `watcher_run_events` tables (the `runs_store` seam) and are
   unit-tested headless; `rich` only renders their output. Swapping the renderer later
   (curses, an optional `textual` app) is a contained change that touches no schema.

When stdout is not a TTY (piped / cron), the dashboard does not run a `rich.Live` loop —
it prints a one-shot JSON snapshot and exits, which keeps the view scriptable.

## Consequences

**Positive**
- Best-in-class terminal UX for the effort; live tables/panels without hand-rolling ANSI.
- The durable parts (the two tables, the run/event capture) are independent of the
  renderer, so the dashboard is replaceable without data changes.

**Negative / costs**
- The package is no longer zero-dependency. README/pyproject reflect "one dependency,
  `rich`, used by the dashboard only." Mitigated: it's confined and the hot path is
  unaffected.
- A non-TTY environment can't show the live view; the JSON fallback covers scripting.

## Alternatives rejected

- **Stdlib ANSI redraw / `curses`** — keeps zero deps but costs hand-rolled layout, flicker
  and resize handling (ANSI) or a fiddlier API and weaker portability (`curses`), for a
  dashboard we want to look good out of the box.
- **`textual` (interactive app framework)** — heavier dependency surface than `rich` and
  oriented at full interactive apps; the monitor is a read-only auto-refreshing readout, so
  `rich.Live` is the right weight. `textual` remains a future option if keyboard/mouse
  drill-down is wanted — it builds on `rich`, so the move is incremental.
