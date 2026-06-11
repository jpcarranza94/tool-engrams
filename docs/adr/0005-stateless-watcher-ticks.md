# ADR-0005 — Watcher ticks are stateless: fresh `claude -p` per tick, no `--resume`

- **Status:** Accepted
- **Supersedes:** the resumed-conversation design in ADR-0001's implementation
  (the *CLI-not-schema* decision of ADR-0001 stands; only the session
  lifetime changes)

## Context

Both watcher roles originally ran one long-lived `claude -p` conversation per
(work session, role): every tick resumed it (`--resume <id>`) and appended the
newest transcript delta. The bet was that accumulated conversation state would
pay for itself — formation could assemble episodes spanning deltas and avoid
re-saving, and evaluation could connect a surface in tick N to its outcome
evidence in tick N+k.

We audited five real watcher conversations (3 formation, 2 eval; 75 formation
ticks, 72 eval judgments, ~$23 of spend) tick by tick:

- **Outcome-changing use of conversation state: formation 4/75 ticks (~5%),
  eval 0–1/72 judgments (~1%).** Every other decision was derivable from the
  current delta plus what the prompt re-presents anyway.
- **Eval's core justification turned out to live in the DB, not the
  conversation.** The pending-surfaces list re-presents memory id, body, and
  trigger every tick; both observed defer-then-judge cases resolved from that
  plus the current delta. When one resume chain broke mid-session
  (`No conversation found`), a fresh conversation picked up 1.5h-stale
  surfaces and judged them seamlessly — an accidental A/B test.
- **The cost structure is the worst case for resume.** Ticks fire hours
  apart; the 5-minute prompt-cache TTL almost never bridges them, so each
  tick re-writes the entire grown history at 1.25× cache-write rates.
  Per-tick cost correlated with conversation length (r≈0.92), not with work
  (r≈0.09 vs delta size). In the worst session, 96% of spend was state
  carriage; measured premiums ranged 2.5–9× vs a fresh-per-tick baseline.
- **Hard failure modes:** long sessions march toward the 200k context
  ceiling; resume ids orphan (33% run-error rate in one session from
  timeouts + a broken chain).
- The counter-evidence even where state *should* have helped: one formation
  session re-announced as a "new pattern" a memory **it had itself saved 15
  ticks earlier in the same conversation** — the stateless CLI dedup caught
  it, the conversation did not.

The ~5% of formation ticks where state genuinely mattered reduce to two
mechanisms, both shallow: (1) pairing evidence from the previous 1–3 deltas
with the current one, and (2) knowing what it already saved this session so a
recurring episode refines the existing body instead of regressing it.

## Decision

**Every tick is a fresh `claude -p` invocation.** The `--resume` plumbing is
deleted (`watcher_state.watcher_session_id`, resume-id extraction, the
broken-chain error class). The two real benefits of state are re-supplied
explicitly and boundedly:

1. **Formation: prior-delta tail.** The tick message includes the tail of the
   previous 1–2 delta windows (re-read from the work transcript via
   `watcher_runs.cursor_from/cursor_to`, capped ~4k chars). No new storage.
2. **Formation: session-saves list.** The tick message lists memories this
   watcher already created/updated for this work session (name, kind,
   trigger — not bodies), hydrated from `watcher_run_events`.
3. **`engram remember` dedup echo.** On `existing_match`, the CLI returns the
   existing memory's current body with an explicit merge instruction, so an
   overwrite is a deliberate merge instead of a blind replace — body-quality
   protection at exactly the moment it matters, statelessly.
4. **Evaluation gets nothing extra.** The re-presented pending list is the
   state. Deferral stays "don't judge yet"; a fresh tick re-derives it.

Sandbox cwds stay (they isolate watcher transcripts and power the
internal-cwd recursion guard — they were never resume infrastructure).
Cursor, coalescing, flush, and idle-sweep semantics are unchanged.

## Consequences

**Positive**
- Watcher spend drops an estimated 60–80%; cost becomes proportional to work
  (delta size), not session age.
- The resume failure class disappears: no orphaned ids, no 200k runway limit,
  no timeout-amplifying re-reads of grown history.
- Each tick is independently debuggable: one message in, one decision out.

**Negative / risks**
- Cross-delta episode assembly now depends on the ~4k delta-tail window; an
  episode spanning more than ~2 ticks without a recap in the work transcript
  will be missed. Accepted: in 75 audited ticks no assembly case reached
  back further than 3 ticks, and work-session recaps usually re-surface the
  context.
- The session-saves list and dedup echo are weaker than full conversational
  memory for body refinement. Accepted: they covered every observed
  refinement case; consolidation remains the deep-merge layer.
- Five sessions, one user's workload. If formation quality regresses, the
  fix is widening the injected tail — not resurrecting resume.
