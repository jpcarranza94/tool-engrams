# ToolEngrams — design v10 (draft, 2026-06-05)

Successor to `design-v9.md`. Planning artifact — argue and edit before code.

Still alpha, no users. Breaking changes are free.

This version is one focused change: **how a memory's usefulness is judged.** Everything
else (kinds, triggers, formation, the hook layout) stays as v9 shipped it.

---

## 1. The bug v9 shipped: success = useful

v9 scores a memory with two counters — `surface_count` (times shown) and
`useful_count` (times judged useful) — combined as a Laplace-smoothed ratio
(`reinforcement/scoring.py`). The counter that matters, `useful_count`, is bumped
in **one wrong place**: the PostToolUse hook (`hooks/post_tool.py`), which credits a
surfaced memory whenever the tool call it surfaced on **succeeds**.

Reads and most commands succeed. So a memory that surfaced on a `Read` and had
nothing to do with the task gets `useful_count++` anyway. The proxy "the call
succeeded" is not "the memory helped."

**Evidence (production DB, from the noise audit):** 101 of 111 active memories sit at
`useful ≈ surface` (usefulness 0.89–1.0 across the board). The signal is saturated —
it cannot tell a good memory from a noisy one. The only honestly-low scores belong to
memories whose calls were *denied/failed*.

The leak that produced the noise is the **path-glob-on-read** path: a memory bound to
`*/Dockerfile` surfaces on every Dockerfile a session reads, regardless of intent, and
each read-success reinforces it.

### 1a. Why the obvious fixes don't suffice

- **"Just stop crediting on success."** Then `block`/PreToolUse memories — which never
  ride a *failure* — have no way to earn usefulness and all decay. The success-credit
  was load-bearing for the wrong reason.
- **"Token-fingerprint the memory's advice and check the next actions."** Too narrow:
  many memories carry no distinctive argument token, and structural advice
  ("provision inside the lock") is invisible to token matching. Heuristics can't judge
  heeding in general.

The judge has to be **intelligent** (read the transcript, decide if the model heeded
the memory) and it has to run at the **right moment** (after the model has had a chance
to act). That is the whole of v10.

---

## 2. The shape: three watcher sessions, one pattern

v9 already had two LLM jobs: **formation** (the event-driven watcher) and
**consolidation** (the nightly agent). v10 adds a third — **evaluation** — and unifies
all three onto a single pattern.

> **A watcher session is a permissioned `claude -p` running in an internal cwd that
> does its job by calling `engram` CLI commands.** No constrained JSON schema. The CLI
> is the interface; the harness stops marshaling model output.

| Watcher | Calls (allowlist) | Job | Cadence | Cursor |
|--|--|--|--|--|
| **formation** | `engram remember` only | create memories from episodes | Stop / flush / recovery | `role='formation'` |
| **evaluation** | `engram judge` only | label how surfaced memories fared | Stop / flush, *only when surfaces pending* | `role='eval'` |
| **consolidation** | full `engram *` | aggregate, narrow triggers, archive, dedupe | nightly | — |

The three differ only in **command surface**, **prompt**, **cadence**, and **cursor**.
Consolidation keeps the destructive/reversible levers (`forget`, `archive`,
trigger-surgery). Evaluation gets exactly one verb. That command-surface restriction —
not a JSON schema — is what keeps a per-turn judge from nuking a good memory on one
twitchy turn.

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

## 3. The evaluation watcher

### 3.1 The moment: next Stop, reading forward

A memory's effect is visible only **after** the surface line. PreToolUse injects a
`block` (deny → the model retries) or a `hint` (`allow` + context → the model proceeds);
in both cases whether the model *heeds* shows in its **subsequent** actions.

```
line 100  PreToolUse → memory M surfaces        (outcome=NULL)
line 101  tool call X runs  (git push --force)  → DENIED by the block
line 105  model reads M, retries: git push --force-with-lease   ← HEEDED here
line 110  success
```

So evaluation cannot fire at surface time reading *backward* — it would see the setup,
never the heeding. It fires at the **next Stop after the surface** and reads **forward**
through the model's response. The eval cursor **trails** the formation cursor by one
turn-boundary: at `Stop_n` it judges surfaces opened before `Stop_n` using evidence
through `Stop_n`.

Heeding that lands many turns later than the surface is missed by the fresh pass —
consolidation's full-trace nightly review is the backstop for that.

### 3.2 Trigger: one hook event, two ticks, gated on pending surfaces

The Stop/flush hooks already spawn the formation tick. They now also spawn the eval
tick — **but only when there is something to judge.** A cheap, hot-path-safe check
before spawning:

```sql
SELECT 1 FROM session_surfaces WHERE session_id = ? AND outcome IS NULL LIMIT 1
```

No pending surfaces → no eval tick, no cursor advance. The eval cursor moves only when
it judges. (Most turns surface nothing, so this bounds eval cost to turns that need it.)

### 3.3 State: `watcher_state` re-keyed by `(work_session_id, role)`

Evaluation mirrors formation's full state — its own cursor, its own resumed
`claude` session id, its own `fail_streak`/held-window retry. Rather than double every
column, `watcher_state`'s primary key becomes `(work_session_id, role)` with
`role ∈ {formation, eval}`: **two symmetric rows per session, same schema.**
`watcher/state.py` reads/writes by `(session, role)`. Migration: existing rows get
`role='formation'`; eval rows are created on demand.

### 3.4 The eval pass: resumed session, CLI verdicts, deferral by omission

Evaluation is a **resumed** `claude -p` session (its own `watcher_session_id`). Resume
solves the "how much prior context to send?" problem a stateless judge would have:
prior context already lives in the session, so each pass sends only the new delta.

Each pass receives:
- the transcript **delta** since the eval cursor (the forward evidence), and
- the **pending-surface list**: `session_surfaces JOIN memories WHERE outcome IS NULL`
  → for each, `{memory_id, name, body, kind, turn_at_surface, first_token}`. The judge
  needs the **body** (the advice) to decide if the model followed it.

For each surface it can conclude, the model calls:

```
engram judge <memory_id> helpful|unused|noise
```

| verdict | meaning (from the model's forward actions) | counter effect |
|--|--|--|
| `helpful` | model visibly followed / used the memory | `useful_count++` |
| `unused`  | memory *was* relevant, model didn't act on it | none (truly neutral) |
| `noise`   | memory had no bearing on the call — the **trigger over-matched** | `noise_count++` |

The `unused`/`noise` split is the discrimination the noise audit wanted: `unused`
protects a correct-but-situational memory; `noise` flags a bad *trigger*.

**Deferral = omission.** A surface whose evidence is still inconclusive: the model simply
**doesn't call `judge`**. The surface stays NULL and is re-listed next pass — and because
the session is resumed, the model still remembers the earlier context, now with more
evidence. No "defer" enum needed.

**Flush is the deadline.** A surface the model never acts on would stay NULL forever. The
flush pass (SessionEnd / PreCompact / idle-sweep) is instructed to judge *all* remaining
pending surfaces, defaulting genuinely-inconclusive ones to `unused`. So mid-session
defers freely; session-end forces closure.

**The parent stops parsing.** The side effects happen in-band via the CLI calls. The
parent just: spawn the resumed eval session → on clean completion advance the eval cursor
→ on failure hold + retry (reuse formation's `_retry_decision`). Partial failure is safe:
`judge` is idempotent (`mark_surface_outcome` already filters `outcome IS NULL`), so a
retry skips done surfaces and re-lists the rest.

### 3.5 `engram judge` — the new verb, the validation boundary

With no schema, the CLI is where misuse is caught. `engram judge <memory_id> <outcome>`:
- rejects an unknown `memory_id`,
- rejects a `memory_id` not in **this session's** pending surfaces,
- rejects an outcome outside `{helpful, unused, noise}`,
- is idempotent — only writes where `outcome IS NULL`,
- sets `session_surfaces.outcome` **and** bumps the memory counter, in one transaction,
- logs to `watcher.log` (observability moves into the CLI — the parent is blind).

Folds the existing `skip` / `mark_noise` labeling paths into one verb.

---

## 4. Scoring redesign

### 4.1 One ratio, honestly fed: `q`

Three non-netted counters on the memory row:

```
surface_count    presentations (PreToolUse)         [exists; telemetry only now]
useful_count     helpful verdicts                   [repurposed: no longer bumped on success]
noise_count      noise verdicts                     [NEW column]
```

A single noise-aware, Laplace-smoothed quality ratio drives **both** ranking and the gate:

```
q = (useful_count + 1) / (useful_count + noise_count + 2)
```

`unused` surfaces enter **neither** counter, so a situational memory (much `unused`,
zero `noise`) keeps `q` high. This is deliberate: the legacy
`usefulness = (useful+1)/(surface+2)` secretly punished situational memories by counting
every presentation in the denominator. `q` does not.

This is **Laplace smoothing** (add-one / rule of succession), not the Laplace
distribution: the `+1 / +2` is a `Beta(1,1)` uniform prior, mean ½. A fresh memory
(`0/0`) sits at exactly `q = 0.5`.

### 4.2 The surfacing gate

v9 never gated — score only **sorted + capped** (`pretool.py`, cap 2). A noisy memory
still surfaced if it placed in the top 2. v10 adds a gate:

- **Gate on `q`, threshold 0.5.** `q < 0.5 ⟺ noise > helpful` — "this memory has proven
  more noise than signal." The threshold is **not a tuned constant**; it's the prior's
  mean. The memory has moved below where it started.
- **Warm-up `N ≈ 3`.** Don't gate until `useful_count + noise_count ≥ N`, so one unlucky
  early `noise` can't kill a new memory. This is the only real knob.
- **`block` and `pinned` are exempt.** A safety rule that's rarely visibly-heeded must
  still fire. The gate is a hint-only quality valve.

Defense in depth: a noisy memory first sinks in the **sort** (lower `q` loses the cap-2
race), then trips the **gate** (`q < 0.5` suppresses it entirely) — one ratio, two effects.

### 4.3 Remove recency from ranking

`final_score` multiplied in `recency = exp(-days_since_last_surface / half_life)`. Drop it.

Surfacing is event-driven: a memory's `last_surfaced_ts` is old precisely when its trigger
hasn't fired — and the moment it fires again it is relevant **now**. Recency down-weighted
a rare-but-important memory at the exact moment it mattered (a 90-day-dormant DST-cron
gotcha decayed to `r ≈ 0.2` on the day it finally triggered). Punishing rarity is backwards
for a memory system.

This removes ranking-by-age, **not** decay: a *wrong* memory (codebase moved on) still gets
archived — by consolidation's quality/staleness audit (driven by `q` + the git-aware check),
which decays only the bad, not the merely-dormant.

**Dead code this retires:**
- `recency()` and `HALF_LIFE_DAYS` (`scoring.py`) — no callers left.
- `structural_match` was already dead (hardcoded 1.0). With recency also gone,
  `final_score` collapses to `(0.5 + q) · [1.5 if pinned]` — pure quality + pin boost. The
  sort becomes `(specificity, q, pinned)`; `final_score` may be retired and the sort done
  on those three directly.
- `last_surfaced_ts` stays as telemetry (still bumped, shown in `recall`), no longer ranks.

---

## 5. Gutting `post_tool.py`

`hooks/post_tool.py` runs two credit paths in one transaction. Both go:

- **Path 1** — pre-tool surface + this call succeeded → `bump_useful` + mark `helpful`.
  **The bug.** Delete.
- **Path 2** — prior *failure* surface, same `first_token`, now succeeded → credit the
  hint (error→fix). A *proxy* ("same first-token retried and succeeded" ≠ "the hint was
  used"). Eval subsumes it and judges actual heeding. Delete the crediting.

**Kept in post_tool:** `increment_session_turn` (the turn counter feeds `turn_at_surface`
and `find_latest_active_session`), and the **recovery fast-path tick** — now a natural
early *eval* trigger too, since the failure-surface's evidence window just closed.

After v10, **the eval watcher is the single writer of `useful_count` / `noise_count` /
`outcome`.** Clean single-seam, matching the per-table seam discipline (`memory_store`,
`session_state`, `consolidation/runs`, `watcher/state`).

`get_prior_failure_surfaces` + the `idx_session_surfaces_failure_token` partial index keep
only their tick-firing consumer; if the armed-at-Stop path already covers recovery, they go
fully dead — decide at build time.

---

## 6. Formation moves to the CLI pattern too

Formation currently uses `--bare` + a constrained `WATCHER_SCHEMA`, returns `{action,
memories[]}`, the harness parses it (`_parse_response`, `_candidate_json_strings`) and
saves via in-process `remember_main` (`_save_memory`). v10 makes formation call
`engram remember` itself, like the other two.

**Delete from the harness** (deletion test — the complexity moves into the CLI seam, it
does not reappear at call sites):
- `WATCHER_SCHEMA`
- `_parse_response`, `_candidate_json_strings`
- `_save_memory` / the in-process `remember_main` call
- the `parse_error` branch + `MODEL-PARSE_ERROR` retry
- `_extract_session_id` (see §7a)

The JSON path was already fragile — three fallback extraction strategies exist *because*
constrained decoding + fenced-JSON parsing breaks in practice. Tool-calls are the model's
native, robust path.

---

## 7. Three mechanism shifts (eyes-open)

**7a. Resume needs the session id, and we no longer parse stdout for it.** Pin a
caller-generated `--session-id <uuid>` per watcher session → we know it for the next
`--resume` without parsing, and `_extract_session_id` dies. *(Confirm `claude -p` honors a
supplied `--session-id` with `--resume`.)*

**7b. Formation retry + tool-calls = duplicate-memory risk.** The held-window retry re-feeds
the same delta; if the model already called `engram remember` before a timeout, the retry
re-creates the memory. **Fix: `engram remember` dedups by name** (`name_exists` already
exists) → re-create becomes update/no-op. (Eval is already safe — idempotent `judge`.)

**7c. Tool-calling can't be `--bare`.** A permissioned session needs granted Bash perms →
the consolidation pattern (`write_agent_settings` + internal temp cwd + `is_internal_cwd`
guard), not `--bare`. Recursion-avoidance moves from `--bare` to the allowlist +
internal-cwd guard; the user's real cwd is passed as `--project-cwd` so project scope still
resolves. *(Confirm `--resume` composes with a permissioned session.)*

**7d. Observability moves into the CLI.** No parsed `MODEL-CREATE` / `MODEL-NONE`; the
parent is blind to what happened. `engram remember` / `judge` log their own actions to
`watcher.log`.

---

## 8. Schema & migration

```sql
-- memories: new counter
ALTER TABLE memories ADD COLUMN noise_count INTEGER NOT NULL DEFAULT 0;

-- cutover: the legacy useful_count is polluted by the success=useful bug
-- (101/111 saturated). It carries no real signal — reset to a clean slate.
UPDATE memories SET useful_count = 0;          -- noise_count defaults to 0
-- surface_count is KEPT (telemetry; lets consolidation see "surfaced 50×, never judged").

-- watcher_state: re-key by (work_session_id, role)
-- existing rows → role='formation'; eval rows created on demand.
```

Every existing memory starts at `q = 0.5` (neutral); the warm-up gate protects them while
eval + consolidation rebuild honest counts. Genuinely-good memories re-earn `helpful`
quickly. `pinned`/`block` are gate-exempt regardless.

---

## 9. Consolidation's expanded role

A `noise` verdict means the **trigger over-matched**, not that the content is bad. So the
nightly agent, reading per-memory outcome distributions (`helpful`/`unused`/`noise`),
**tries trigger-surgery before archiving**: narrow a broad glob, drop a redundant path on a
hybrid that already has a command trigger, rebind to the real command moment. It archives
only when the *content* is useless. This is the noise audit's EDIT/CMD/DOC/NARROW actions,
now driven by live noise data instead of a one-time manual pass.

**CLI gap:** to narrow a trigger without `forget`+recreate (which loses the memory's history
and counters), add an `engram trigger` verb (add / remove / replace a trigger on an existing
memory). Consolidation-prompt change + that CLI addition. Slow-lever follow-on — lands with
or just after the core eval redesign, not a blocker.

---

## 10. Decisions (rationale captured; see `docs/adr/`)

- **Watchers evaluate by calling the `engram` CLI, not by returning a constrained JSON
  schema.** Robust (native tool-calling vs fragile JSON parsing), deferral falls out of
  not-calling, partial failure is safe, and it unifies all three watchers. Safety is held by
  restricting each watcher's **command surface**, not by a schema. → ADR-0001.
- **Recency is removed from ranking.** Event-driven surfacing makes age a backwards signal;
  staleness is consolidation's job, driven by quality + a git-aware audit. → ADR-0002.

---

## 11. Build order

1. **Schema + migration** — `noise_count`, `watcher_state` re-key, reset `useful_count`.
2. **`engram judge`** — the verb + validation + idempotency + logging (unit-testable seam).
3. **Scoring** — `q`, the gate (threshold 0.5, warm-up N), remove recency + dead code.
4. **Gut `post_tool.py`** — delete both credit paths; eval becomes sole writer.
5. **Eval watcher** — `(session, role)` state, surfaces-pending trigger, resumed `--bare`-less
   session, the eval prompt, deferral + flush deadline.
6. **Formation → CLI** — `engram remember` self-call, `--session-id` pinning, name dedup,
   delete the JSON harness.
7. **Consolidation** — outcome aggregation, trigger-narrowing, `engram trigger`.

1–4 are the bug fix and are independently shippable. 5–6 are the watcher rework. 7 is the
slow-lever follow-on.
