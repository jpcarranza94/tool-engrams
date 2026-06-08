# ADR-0002 — Recency is not part of memory ranking

- **Status:** Accepted
- **Context doc:** `docs/design.md` §3.3

## Context

A natural way to decay stale memories is a recency term in the ranking score —
`recency = exp(-days_since_last_surface / half_life)` — so a memory not seen in a while
ranks lower.

But surfacing is **event-driven**: a memory only surfaces when its trigger matches a
tool call. So `last_surfaced_ts` is old precisely when the user hasn't done that kind of
action lately — and the moment they do it again, the trigger fires and the memory is
relevant **now**. A recency term would down-weight the memory at the exact moment it
mattered.

Concretely: a "use `datetime.combine`, not `.replace(hour=)`" gotcha bound to a DST cron
that comes up once a quarter would have decayed to `r ≈ 0.2` by the day its trigger
finally fired again — demoting it out of the surface race right when it was needed.

## Decision

**Ranking has no recency term.** It is driven by trigger specificity and `q` (noise-aware
usefulness) only; `final_score = (0.5 + q) × [1.5 if pinned]`.

This avoids ranking-by-age, **not** decay of wrong memories. A memory whose content the
codebase has moved past is still retired — by consolidation's quality/staleness audit,
which is driven by `q` plus a git-aware check. That decays the *bad*, not the merely
*dormant*.

## Consequences

**Positive**
- Rare-but-important memories are not punished for rarity — the core promise of a memory
  system.
- `last_surfaced_ts` remains as telemetry (shown in `recall`) but does not rank.

**Negative / risks**
- Staleness has no passive, automatic decay — it depends on consolidation running and
  catching wrong/outdated memories. Mitigated: consolidation exists and the git-aware
  staleness audit is its job.
- A genuinely obsolete memory whose trigger still fires keeps surfacing until
  consolidation archives it. Acceptable: the surfacing gate (`q < 0.5`) and the nightly
  audit both act on it; a blunt age-decay would have hit good rare memories too.
