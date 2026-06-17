# ADR-0014 — Formation surfaces near-duplicates and withholds the insert for review

- **Status:** Accepted
- **Context for:** the same consolidation run, which flagged same-named /
  same-idea duplicate memories the formation pipeline kept creating

## Context

`find_overlapping_memory` (dedup.py) merges a new memory into an existing one
only when they **share a trigger in scope**. A memory worded the same but bound
to *different* triggers slips past it: the live DB had `macos-no-timeout-command`
×3, all active, same name, non-overlapping triggers. The nightly consolidator
can't cleanly prune these either — `forget`/`edit`/`trigger` resolve a name to
one arbitrary row, so it can't target a specific duplicate.

The robust place to stop a duplicate is at formation, before it exists — and the
right judge of "is this the same memory?" is the formation agent (an LLM),
given the candidates. But the formation watcher is **remember-only**
(`ROLE_ALLOWED_VERBS["formation"] = ("engram remember",)`, ADR-0010 containment):
it cannot `edit` or `forget` to clean up a dup it just created. So an
advisory-after-insert ("here are similar ones") would leave the dup stranded —
the agent has no verb to act on the advice.

## Decision

`engram remember` grows a **semantic near-duplicate gate** on the would-insert
path (the trigger-overlap auto-merge is unchanged and runs first):

1. `formation.find_similar` returns the top-3 textually similar active memories
   — FTS5/BM25 shortlist over name/description/body, re-scored by token-Jaccard
   (a normalized 0–1; BM25 `rank` isn't comparable across queries). Stdlib + the
   existing FTS index, no new dependency.
2. If the top score ≥ `SIMILARITY_THRESHOLD` (0.6) and neither `--force` nor
   `--into` was passed, **nothing is inserted**: the CLI returns
   `action: "review_similar"` with the candidates and guidance.
3. The agent — staying within the `remember` verb — chooses:
   - `engram remember --into <id> "<merged body>"` → fold into that memory
     (keeps its id, counters, surfaces);
   - `engram remember … --force` → insist it's genuinely new;
   - do nothing.
4. Below threshold, the insert proceeds and the top-3 ride along as
   `similar_memories` (advisory).

The formation prompt documents the `review_similar` response and the three
choices.

## Alternatives considered

- **Advisory-only (always insert, attach top-3):** what the formation agent
  *can't* act on, because it's remember-only — the dup persists. Rejected on the
  containment reality.
- **Widen formation's allowed verbs to include `edit`/`forget`:** lets the agent
  self-correct, but grows the watcher's blast radius (a hijacked formation tick
  could rewrite/archive memories) — exactly what ADR-0010 narrowed. Rejected.
- **`UNIQUE(name, scope, project_slug)` constraint:** a hard guarantee, but names
  are LLM-chosen and a true near-dup often has a slightly different name, so it
  both over- and under-fires; needs a migration + dedup of existing rows. The
  semantic gate catches the real cases (different name, same idea) a name unique
  index would miss.
- **Embedding similarity:** higher recall, but a model/network dependency on a
  background path that is deliberately stdlib + sqlite. FTS+Jaccard is enough to
  flag the obvious dupes.

## Consequences

- New duplicates are caught at the source; the agent merges or forces with a
  clear, one-call response. Trigger-overlap dedup is unaffected.
- The gate fires for **all** `engram remember` callers (manual too), which is a
  feature — `--force` is the explicit override; `--dry-run` reports the
  neighbors without gating.
- Pre-existing duplicates in the store are not touched by this change — they
  remain a consolidation/cleanup concern (and motivate a name-ambiguity guard on
  the name-keyed verbs, tracked separately).
- `SIMILARITY_THRESHOLD = 0.6` is a tuning knob; too high lets near-dups through,
  too low blocks legitimately-distinct memories behind `--force`.
