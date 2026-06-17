# ADR-0013 — useful_count/noise_count derive from session_surfaces (surface-truth)

- **Status:** Accepted
- **Context for:** a nightly consolidation run flagging that `q` (the surfacing
  gate's quality ratio) had drifted below memories' real helpfulness

## Context

The PreToolUse surfacing gate ranks/suppresses memories by
`q = (useful_count + 1) / (useful_count + noise_count + 2)`. A consolidation run
found `q` systematically *under*-crediting genuinely helpful memories: 18 active
memories were gate-suppressed (`q < 0.5`) despite being net-helpful by their
actual `session_surfaces` outcomes — e.g. id 25 had **37 helpful / 6 noise**
surfaces but `q = 0.36`; several had `useful_count = 0` against 10–15 helpful
surfaces.

`session_surfaces.outcome` ('helpful'/'unused'/'noise') is the durable,
per-surfacing ground truth. The counters had diverged from it three ways:

1. **The v12 migration** added `noise_count` (at 0) and `UPDATE memories SET
   useful_count = 0` — wiping pre-v12 helpfulness while the surfaces survived.
2. **`restore` zeroed `useful_count`** even though `archive` never touched the
   counters, so an archive→restore round-trip silently reset a proven memory to
   neutral `q = 0.5`.
3. **`judge` bumped the counter +1 per call** while `mark_surface_outcome`
   closed *N* pending surface rows — so the counter tracked judge *events*, not
   helpful *surfaces*, and lagged badly for frequently-surfaced memories.

## Decision

Make the counters a **cached projection of `session_surfaces`**: `useful_count`
= count of `outcome='helpful'` rows, `noise_count` = count of `outcome='noise'`
rows. ('unused' counts for neither — relevant-but-not-acted-on is not a
negative signal.)

- **`memory_store.recount_from_surfaces(ids|None)`** — recompute the counters
  from the surface table for the given memories (or all). Idempotent.
- **`engram rebuild-counters`** — one-shot CLI that recomputes every memory and
  reports the drift it healed (`--dry-run` to preview). Mirrors
  `rebuild-triggers`.
- **`judge` bumps by rows-closed** (`bump_useful/noise(..., delta=updated)`)
  instead of +1 — so ongoing judging stays consistent with the recompute. A
  re-judge closes 0 rows → +0, still idempotent.
- **`restore` recomputes** from surfaces instead of zeroing.

## Alternatives considered

- **Keep counters as judge-event tallies, fix only the migration reset:** the
  per-event vs per-row unit mismatch would keep re-drifting after any rebuild,
  and `q` would still under-credit frequently-helpful memories. The two units
  have to agree; surface-truth is the one the consolidator already trusts.
- **Compute `q` live from `session_surfaces` at gate time (drop the counters):**
  the gate is the hot path (single-digit-ms SQL); a per-call aggregate over the
  surface table is more work than reading two columns. Caching the projection on
  `memories` keeps the read trivial; the recompute is the write-time cost.
- **Count distinct sessions instead of rows:** dampens one chatty session
  inflating a memory, but it isn't what `session_surfaces` records, so it can't
  be rebuilt deterministically. Row counts are the honest, reproducible signal;
  the `q` gate (`>0.5`) is robust to mild inflation.

## Consequences

- `q` now reflects the real helpful-vs-noise surface ratio; the 18 suppressed-
  but-helpful memories surface again after a `rebuild-counters`.
- `rebuild-counters` is a manual repair (run once now; safe to re-run). Forget's
  deliberate soft-demote (`useful_count = 0`) is overridden by a subsequent
  rebuild — acceptable, since a demoted-but-still-surfacing memory will re-earn
  its outcomes, and hard suppression is `archive`/quarantine's job.
- `surface_count` remains telemetry only (times shown), never a quality input —
  unchanged.
- **Concurrency:** `rebuild-counters` recomputes inside a single
  `UPDATE … (subquery over session_surfaces)` statement, so under SQLite's
  statement-level locking (+ `busy_timeout`) it and a concurrent `judge` bump
  serialize. The only write a rebuild could clobber is a surface row that landed
  after its statement began — and the next rebuild re-derives it. Since
  `rebuild-counters` is a human-driven one-shot, the window is immaterial.
