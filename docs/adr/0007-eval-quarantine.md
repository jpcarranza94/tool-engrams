# ADR-0007 — Evaluation may quarantine a harmful memory; it may not forget or edit

- **Status:** Accepted
- **Extends:** the containment model of ADR-0001 and the README Security
  section

## Context

A memory whose *content* is actively harmful (Claude followed it and broke
something; it encodes a dangerous or simply wrong instruction) has no fast
path out of circulation. The surfacing gate needs `useful_count +
noise_count ≥ 3` before it can suppress; nightly consolidation may be hours
away. The evaluation watcher sees the damage live — it reads the transcript
right after the memory influenced a call — but its only verb is `judge`,
which can merely add one `noise` count.

The obvious fix — adding `engram forget` to eval's allowlist — breaks the
containment boundary. The boundary is a **command-prefix allowlist, not a
schema** (ADR-0001): allowing `engram forget` also allows `--delete` (hard
archive) and `--topic` (fuzzy bulk demotion). A background sonnet process
would gain destructive, fuzzy-matching power, voiding the README's promise
that a hijacked watcher "can write bad memories (auditable, reversible) but
can't do worse." The same argument keeps `engram edit` out of both watcher
allowlists: autonomously rewriting existing trusted bodies is tampering
surface, strictly worse than writing new auditable rows.

## Decision

**Eval gains exactly one new verb: `engram quarantine <id> --reason "…"`.**

By construction it can only do three reversible, audited things:

1. Soft-demote the memory (the existing `forget` non-delete semantics —
   recoverable with `engram forget --restore`).
2. Record a `quarantined` event in `watcher_run_events` with the reason —
   the audit trail consolidation and `engram monitor` read.
3. Mark the memory's latest unjudged surface `noise`, so the reinforcement
   signal reflects the incident.

It addresses memories by id only (no fuzzy name matching), one at a time (no
bulk), and cannot hard-delete. The eval prompt instructs: quarantine only on
*demonstrable* harm in the transcript — followed-and-broke-something, or
dangerous content — never for mere irrelevance (that is what `noise` is
for). Nightly consolidation reviews quarantined memories with full-day
context and either restores, repairs (it holds `edit`-equivalent powers via
its own allowlist), or archives.

`engram edit` ships alongside for **interactive sessions and consolidation
only**: in-place body/name/description correction preserving id, counters,
surfaces, and triggers (the counter-preserving analogue of `engram trigger`
narrowing), setting `last_verified_ts` because a deliberate correction is
the strongest freshness signal the staleness audit can get.

## Consequences

**Positive**
- Harm response latency drops from "nightly" to "next eval tick" while every
  power granted stays reversible, audited, and id-scoped.
- The destructive verbs (`forget --delete`, bulk `--topic`, body rewriting)
  remain exclusively human/consolidation-tier.
- The lifecycle asymmetry closes: triggers had counter-preserving repair
  (`engram trigger`), now bodies do too (`engram edit`) — ending the
  forget-and-re-remember dance that destroyed reinforcement history and
  could be interrupted halfway.

**Negative / risks**
- A miscalibrated eval could quarantine good memories. Bounded: soft-demote
  is reversible, every event carries a reason, and consolidation re-reviews
  nightly. A trigger-happy pattern would show up in `engram monitor`.
- One more verb in eval's allowlist grows its attack surface. Accepted: the
  verb's worst case (wrongly demoting one memory, audited) is the same class
  as `judge noise`, just stronger — not a new class.
