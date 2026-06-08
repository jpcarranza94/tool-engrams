# ToolEngrams — design

How the system decides what to remember, what to surface, and how good a memory is.
This document is the rationale companion to the README: the README says *what* the
pieces are, this says *why* they work the way they do.

---

## 1. Three watcher sessions, one pattern

Three LLM jobs maintain the memory store, and they all run the same way:

> A **watcher session** is a permissioned `claude -p` running in an internal cwd that
> does its job by calling `engram` CLI commands. There is no constrained JSON schema —
> the CLI is the interface, and the harness never marshals model output.

| Watcher | Command surface | Job | Cadence | Cursor |
|--|--|--|--|--|
| **formation** | `engram remember` only | create memories from episodes | Stop / flush / recovery | `role='formation'` |
| **evaluation** | `engram judge` only | label how surfaced memories fared | Stop / flush, only when surfaces are pending | `role='eval'` |
| **consolidation** | full `engram *` | aggregate, narrow triggers, archive, dedupe | nightly | — |

They differ only in **command surface**, **prompt**, **cadence**, and **cursor**.
Consolidation keeps the destructive/reversible levers (`forget`, `archive`,
trigger-surgery). Evaluation gets exactly one verb. That command-surface restriction —
not a JSON schema — is what keeps a per-turn judge from nuking a good memory on one
twitchy turn (see ADR-0001).

```
            PreToolUse                 Stop / flush
   tool call ───▶ surface memory   ┌────────────────────────┐
                  (outcome=NULL)    │                        │
                        │           ▼                        ▼
                        │    formation tick            eval tick (if pending)
                        │    `engram remember`         `engram judge <id> <verdict>`
                        │           │                        │
                        ▼           ▼                        ▼
                  session_surfaces ─┴── memories ◀───────────┘
                                          │
                                    nightly consolidation
                                    aggregate → narrow / archive
```

---

## 2. The evaluation watcher

A memory's usefulness is whether the model **heeded** it — which can only be judged by
reading the transcript, not inferred from whether the tool call succeeded. (Reads and
most commands succeed, so "the call succeeded" reinforces any memory that happened to
surface, regardless of relevance. That is exactly the noise a path-glob memory bound to
`**/Dockerfile` produces: it fires on every Dockerfile a session reads.) The judge has
to be **intelligent** and run at the **right moment**.

### 2.1 The moment: the next Stop, reading forward

A memory's effect is visible only **after** the surface line. PreToolUse injects a
`block` (deny → the model retries) or a `hint` (`allow` + context → the model proceeds);
in both cases whether the model heeds shows in its **subsequent** actions.

```
line 100  PreToolUse → memory M surfaces        (outcome=NULL)
line 101  tool call X runs  (git push --force)  → DENIED by the block
line 105  model reads M, retries: git push --force-with-lease   ← HEEDED here
line 110  success
```

So evaluation fires at the **next Stop after the surface** and reads **forward** through
the model's response. The eval cursor trails the formation cursor by one turn-boundary:
at `Stop_n` it judges surfaces opened before `Stop_n` using evidence through `Stop_n`.
Heeding that lands many turns later is caught by consolidation's full-trace nightly
review.

### 2.2 Trigger: gated on pending surfaces

The Stop/flush hooks spawn the eval tick **only when there is something to judge** —
a cheap, hot-path-safe check before spawning:

```sql
SELECT 1 FROM session_surfaces WHERE session_id = ? AND outcome IS NULL LIMIT 1
```

No pending surfaces → no eval tick, no cursor advance. Most turns surface nothing, so
this bounds eval cost to the turns that need it.

### 2.3 State: `watcher_state` keyed by `(work_session_id, role)`

Evaluation mirrors formation's full state — its own cursor, its own resumed `claude`
session id, its own held-window retry streak. Rather than double every column, the
`watcher_state` primary key is `(work_session_id, role)` with `role ∈ {formation,
eval}`: two symmetric rows per session, same schema, both reached through
`watcher/state.py`.

### 2.4 The pass: resumed session, CLI verdicts, deferral by omission

Evaluation is a **resumed** `claude -p` session. Resume solves the "how much prior
context to send?" problem: prior context already lives in the session, so each pass
sends only the new delta plus the current pending-surface list (`session_surfaces JOIN
memories WHERE outcome IS NULL` → `{memory_id, name, body, kind, turn_at_surface,
first_token}` — the judge needs the **body** to decide if the model followed the advice).

For each surface it can conclude, the model calls `engram judge <memory_id>
helpful|unused|noise`:

| verdict | meaning (from the model's forward actions) | counter effect |
|--|--|--|
| `helpful` | model visibly followed / used the memory | `useful_count++` |
| `unused`  | memory *was* relevant, model didn't act on it | none (truly neutral) |
| `noise`   | memory had no bearing on the call — the **trigger over-matched** | `noise_count++` |

The `unused`/`noise` split is the key discrimination: `unused` protects a
correct-but-situational memory; `noise` flags a bad *trigger*.

**Deferral is omission.** If a surface's evidence is still inconclusive, the model simply
**doesn't call `judge`** for it. The surface stays NULL and is re-listed next pass —
and because the session is resumed, the model still remembers the earlier context, now
with more evidence. No "defer" verdict is needed.

**Flush is the deadline.** The flush pass (SessionEnd / PreCompact / idle-sweep) is
instructed to judge *all* remaining pending surfaces, defaulting genuinely-inconclusive
ones to `unused`. So mid-session defers freely; session-end forces closure.

The parent never parses output. It just spawns the resumed eval session, advances the
eval cursor on clean completion, and holds + retries on failure (the same
`_retry_decision` formation uses). Partial failure is safe: `judge` is idempotent
(`mark_surface_outcome` only writes `outcome IS NULL`), so a retry skips done surfaces
and re-lists the rest.

### 2.5 `engram judge` — the validation boundary

With no schema, the CLI is where misuse is caught. `engram judge <memory_id> <outcome>`:

- rejects an unknown `memory_id`,
- rejects a `memory_id` not in **this session's** surfaces,
- rejects an outcome outside `{helpful, unused, noise}`,
- is idempotent — only writes where `outcome IS NULL`,
- sets `session_surfaces.outcome` **and** bumps the memory counter in one transaction,
- logs its action to `watcher.log`.

---

## 3. Scoring

### 3.1 One ratio, honestly fed: `q`

Three non-netted counters live on the memory row:

```
surface_count    presentations (PreToolUse)   — telemetry only
useful_count     helpful verdicts
noise_count      noise verdicts
```

A single noise-aware, Laplace-smoothed quality ratio drives **both** ranking and the gate:

```
q = (useful_count + 1) / (useful_count + noise_count + 2)
```

`unused` surfaces enter **neither** counter, so a situational memory (much `unused`,
zero `noise`) keeps `q` high — it is not punished for being correctly quiet. This is
**Laplace smoothing** (add-one / rule of succession), not the Laplace distribution: the
`+1 / +2` is a `Beta(1,1)` uniform prior with mean ½, so a fresh memory (`0/0`) sits at
exactly `q = 0.5`.

### 3.2 The surfacing gate

Ranking alone only **sorts and caps** — a noisy memory still surfaces if it places in
the top N. The gate suppresses it outright:

- **Gate on `q`, threshold 0.5.** `q < 0.5 ⟺ noise > helpful` — the memory has proven
  more noise than signal. The threshold is not a tuned constant; it is the prior's mean,
  i.e. "the memory has dropped below where it started."
- **Warm-up `N = 3`.** Don't gate until `useful_count + noise_count ≥ N`, so one unlucky
  early `noise` can't kill a young memory. This is the only real knob.
- **`block` and `pinned` are exempt.** A safety rule that's rarely visibly-heeded must
  still fire. The gate is a hint-only quality valve.

Defense in depth: a noisy hint first sinks in the **sort** (lower `q` loses the cap
race), then trips the **gate** (`q < 0.5` suppresses it entirely) — one ratio, two
effects.

### 3.3 No recency in ranking

`final_score` is `(0.5 + q) × [1.5 if pinned]` — quality plus a pin boost, nothing else.
There is deliberately no age term.

Surfacing is event-driven: a memory's `last_surfaced_ts` is old precisely when its
trigger hasn't fired — and the moment it fires again it is relevant **now**. An age decay
would down-weight a rare-but-important memory at the exact moment it mattered (a
quarter-yearly DST-cron gotcha would have decayed to near-zero by the day its trigger
finally fired again). Punishing rarity is backwards for a memory system (see ADR-0002).

This drops ranking-by-age, not decay of *wrong* memories: a memory whose content the
codebase has moved past is still retired — by consolidation's quality/staleness audit,
which is driven by `q` plus a git-aware check. That decays the bad, not the merely
dormant. `last_surfaced_ts` remains as telemetry (shown in `recall`), it just doesn't
rank.

---

## 4. PostToolUse: turn counter + recovery tick

`hooks/post_tool.py` does **not** judge usefulness — that is the evaluation watcher's
sole job. It keeps two responsibilities:

- `increment_session_turn` — the per-session tool-call counter that feeds
  `turn_at_surface` and `find_latest_active_session`.
- the **recovery fast-path tick** — when a prior failure surface's `first_token` just
  succeeded, an error→fix episode is provably present and that surface's evidence window
  just closed. It fires both a formation tick (the episode completed) and an eval tick
  (the failure surface can now be judged), instead of waiting for the next Stop.

This makes the evaluation watcher the single writer of `useful_count` / `noise_count` /
`session_surfaces.outcome`, matching the per-table seam discipline (`memory_store`,
`session_state`, `consolidation/runs`, `watcher/state`).

---

## 5. Formation via the CLI

Formation is a permissioned `claude -p` whose only granted command is `engram remember`.
It reads the transcript delta and runs `engram remember …` for patterns worth keeping,
passing the user's real cwd through `--project-cwd` so a project-scoped memory binds to
the user's repo rather than the watcher's working directory. Native tool-calling is the
model's robust path; there is no JSON to parse and nothing for the harness to marshal.

Two mechanisms keep formation safe:

- **Duplicate-memory safety.** The held-window retry can re-feed the same delta; if the
  model already created a memory before a timeout, `engram remember` recognizes the
  existing name/triggers and updates in place rather than duplicating.
- **Recursion guard.** A permissioned (non-`--bare`) session runs with hooks active, so
  its own `engram` tool calls would otherwise re-enter the hook layer. `ENGRAM_IN_WATCHER`
  (set on the session's env) plus the internal-cwd check make every hook a no-op inside a
  watcher child.

---

## 6. Schema

- **memories** — content, `kind ∈ {block, hint}`, `scope ∈ {global, project}`, and the
  counters `surface_count` (telemetry), `useful_count`, `noise_count`, plus
  `last_surfaced_ts`, `pinned`, `archived_ts`, `last_verified_ts`.
- **triggers** — `kind ∈ {token_subseq, path_glob}`; `token_subseq` stores `first_token`
  (indexed) + `tokens_json`, `path_glob` stores an fnmatch pattern.
- **session_surfaces** — which memories surfaced, when, under which hook, and the
  evaluation watcher's `outcome ∈ {helpful, unused, noise}` (NULL until judged).
- **session_turns** — per-session tool-call counter.
- **consolidation_runs** — nightly run log with quality metrics.
- **watcher_state** — keyed `(work_session_id, role)`: per-role transcript cursor, resume
  id, and tick state (`armed`, `last_tick_ts`, `fail_streak`).

Every existing memory starts at `q = 0.5`; the warm-up gate protects fresh memories while
the evaluation watcher and consolidation build honest counts.

---

## 7. Consolidation's role

A `noise` verdict means the **trigger over-matched**, not that the content is bad. So the
nightly agent, reading per-memory outcome distributions (`helpful`/`unused`/`noise`),
tries **trigger-surgery before archiving**: narrow a broad glob, drop a redundant path on
a memory that already has a precise command trigger, or rebind to the real command
moment. It archives only when the *content* is useless.

`engram trigger` is the lever for this: it adds/removes/replaces a trigger on an existing
memory while preserving its `useful_count` / `noise_count` history (a `forget` + new
`remember` would reset them). It refuses to leave a memory with zero triggers.

---

## 8. Key decisions

Recorded as ADRs in `docs/adr/`:

- **ADR-0001** — watcher sessions evaluate by calling the `engram` CLI, not by returning
  a constrained JSON schema. Safety is the command surface, not a schema.
- **ADR-0002** — recency is removed from ranking. Event-driven surfacing makes age a
  backwards signal; staleness is consolidation's job.
