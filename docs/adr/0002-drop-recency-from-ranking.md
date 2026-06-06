# ADR-0002 — Remove recency from memory ranking

- **Status:** Accepted (2026-06-05)
- **Context doc:** `docs/design-v10.md` §4.3

## Context

v9's `final_score` multiplied in a recency term,
`recency = exp(-days_since_last_surface / half_life)` (half-life 30d for `block`, 60d
for `hint`). The intent was to decay stale memories.

But surfacing is **event-driven**: a memory only surfaces when its trigger matches a
tool call. So `last_surfaced_ts` is old precisely when the user hasn't done that kind of
action lately — and the moment they do it again, the trigger fires and the memory is
relevant **now**. Recency down-weighted the memory at the exact moment it mattered.

Concretely: a "use `datetime.combine`, not `.replace(hour=)`" gotcha bound to a DST cron
that comes up once a quarter would have decayed to `r ≈ 0.2` by the day its trigger
finally fired again — demoting it out of the cap-2 surface race right when it was needed.

## Decision

**Remove the recency term from ranking.** Ranking is driven by trigger specificity and
`q` (noise-aware usefulness) only.

This removes ranking-by-age, **not** decay. A memory that is *wrong* (the codebase moved
on) is still retired — by consolidation's quality/staleness audit, which is driven by
`q` plus a git-aware check. That decays the *bad*, not the merely *dormant*.

## Consequences

**Positive**
- Rare-but-important memories are no longer punished for rarity — the core promise of a
  memory system.
- Dead code retired: `recency()` and `HALF_LIFE_DAYS` lose their callers. With
  `structural_match` already dead (hardcoded 1.0), `final_score` collapses to
  `(0.5 + q) · [1.5 if pinned]`; the sort reduces to `(specificity, q, pinned)`.
- `last_surfaced_ts` remains as telemetry (shown in `recall`) but no longer ranks.

**Negative / risks**
- Staleness no longer has a passive, automatic decay — it depends on consolidation
  running and catching wrong/outdated memories. Mitigated: consolidation already exists
  and the git-aware staleness audit is its job.
- A genuinely obsolete memory whose trigger still fires will keep surfacing until
  consolidation archives it. Acceptable: the surfacing gate (`q < 0.5`) and the nightly
  audit both act on it; blunt age-decay would have hit good rare memories too.
