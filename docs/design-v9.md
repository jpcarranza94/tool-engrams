# ToolEngrams — design v9 (draft, 2026-04-21)

Successor to `design-v8.md`. This doc is a planning artifact — review, argue, edit before any code changes.

Still under alpha development. No users yet. We have freedom to make breaking changes.

---

## 1. What we learned from v1 (= shipped v8)

v8 shipped the core storage + retrieval + watcher + consolidation machinery. Then we used it, and a few things became clear:

**1a. Two memory types aren't doing useful work.** The `feedback` vs `reference` split was meant to separate "block on match" from "just inject context." In practice:

- `reference` memories inject on *every* matching PreToolUse. The noise-to-signal ratio is bad: most calls that match a reference memory don't actually need the context. Claude knows.
- `feedback` memories deny calls pre-hook. This works for the ergeon-cli style "use the other flag" case, but the deny UX is harsh — we block calls Claude might intentionally want to run (e.g. WIP force-push).
- Claude Code's own permission system is a better fit for deny semantics anyway (rule-based, explicit, scoped to settings). Memory-based deny overlaps with it without being better.

**1b. Prefix matching on head tokens breaks for real CLIs.** `ergeon order 12345 reassign` can't match a trigger `["ergeon", "order", "reassign"]` because `12345` sits between the matching tokens. Any subcommand CLI with positional IDs before verbs has this shape. `gh pr 123 comment`, `jira sprint 5 add`, `kubectl get pod-abc123 -o json` — all real.

**1c. PreToolUse-on-every-call is the wrong surface moment for most memories.** The engineer's-hallway-conversation insight: memory is most useful *right after* Claude makes a mistake — when the correction is about to change behavior. Pre-call injection is low signal because the model often already knows; post-failure injection is high signal because the failure itself demonstrates the gap.

**1d. Hardcoded watcher/consolidation prompts are a packaging bug.** For an OSS tool, users need to override the formation logic without forking.

## 2. Positioning

**One-line pitch:** ToolEngrams makes agent-facing tools self-documenting through interaction. Every failed tool call is a chance to remember something — next session doesn't need to rediscover it.

**Canonical use case (lead example in README):** custom CLIs not in training data (ergeon-cli, internal gh-like tools, bespoke cmd-line wrappers). Agent explores, fails, corrects; system remembers the corrections; subsequent sessions arrive warm.

**Also works for:** database schema discovery, API endpoint discovery, internal framework patterns — any case where an agent interacts with a surface it doesn't know.

**Explicitly not trying to solve:**
- Semantic errors that return exit 0 (e.g. SQL that runs but joins wrong columns). We'd need LLM-in-the-loop at tool-call time, which breaks the "no LLM on the hot path" constraint.
- Conversational/RAG-style memory. Different problem, different tools (mem0, etc.) do it already.
- Destructive-command blocking. Claude Code permissions are the right tool. We keep a narrow `block` mode but don't market the system around it.

## 3. The v2 model

Two memory kinds, one trigger mechanism, two surface moments.

### 3.1 Memory kinds

```
kind: block | hint
```

| Kind    | Fires at       | Effect                                 | When to use                                          |
|---------|----------------|----------------------------------------|------------------------------------------------------|
| `block` | PreToolUse     | Denies the call, injects message       | Rare. Only for things you actively want to prevent. |
| `hint`  | PostToolUse    | Injects `additionalContext` on failure | Default for everything else.                         |

`hint` only fires when the tool call's **exit code is non-zero** (or for tools without exit codes, when the output contains an explicit error marker — see §5). This is the core constraint that makes the system non-noisy.

`block` is kept for narrow cases (e.g. user explicitly wants to prevent `rm -rf /`-style mistakes in a project) but we expect most users to create zero of these. The README will present it as an edge-case feature, not a headline.

### 3.2 Triggers: required-token subsequence match

Replaces v8's `head_joined` prefix match.

A trigger is a list of **required tokens in order**. A tool call matches if all trigger tokens appear in the call's tokenization in the given order, not necessarily contiguously.

```
trigger:         ["ergeon", "order", "reassign"]
call (matches):  ergeon order 12345 reassign
call (matches):  ergeon --env staging order abc reassign --reason X
call (no match): ergeon order reassign        <-- missing — wait, this DOES match (3 tokens present in order, no gap required)
call (no match): ergeon reassign order         <-- wrong order
call (no match): ergeon customer reassign      <-- "order" missing
```

This handles the positional-ID-between-verbs case cleanly.

**Implementation:** at match time, fetch all memories for the call's first token (cheap — single indexed lookup), do the subsequence check in Python. For DB volumes in the hundreds-to-low-thousands of memories per first-token bucket, this is single-digit ms, same budget as v8.

**Path triggers:** unchanged from v8 — `path_glob` triggers for file-based tools (Read, Edit, Write) still use `fnmatch`. No reason to change; paths aren't prefix/subsequence-shaped.

**Regex triggers:** deferred. Not needed for v2. Can be added as a third trigger kind later if real demand surfaces.

### 3.3 Surface moments

Only two, each dead simple:

**PreToolUse hook:**
- Look up `block` memories whose triggers match the current call.
- If any match → emit `permissionDecision: deny` + `additionalContext` with the memory body.
- If none → emit `{}` (noop).
- **Does not fire `hint` memories.** Ever.

**PostToolUse hook:**
- Only runs retrieval if `is_error` is true (exit code != 0, error marker detected, etc — see §5).
- On error: look up `hint` memories whose triggers match the failed call.
- Inject matching memories as `additionalContext` on PostToolUse output.
- On success: does nothing retrieval-wise (only reinforcement bookkeeping, see §6).

That's it. No SessionStart pinned-memory surfacing in v2 (we can add it back if dogfooding shows value). No UserPromptSubmit retrieval. No Hebbian associative surfacing. Just the two hooks above.

**What we gain by dropping the other surface moments:** enormous conceptual simplification. A new user can read `hooks/pretool.py` and `hooks/post_tool.py` and understand the entire surface behavior in 10 minutes. The v1 version has associative tracks, Laplace thresholds, cluster stats, scope filters — all of that remains as machinery but only for ranking within the two simple surface moments, not as additional moments themselves.

## 4. Data model

```sql
-- memories: one row per stored memory
CREATE TABLE memories (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  description     TEXT,
  body            TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('block', 'hint')),  -- replaces `type`
  scope           TEXT NOT NULL CHECK (scope IN ('global', 'project')),
  project_slug    TEXT,
  created_ts      INTEGER NOT NULL,
  last_surfaced_ts INTEGER DEFAULT 0,
  surface_count   INTEGER DEFAULT 0,
  useful_count    INTEGER DEFAULT 0,
  pinned          INTEGER DEFAULT 0,
  archived_ts     INTEGER
);

-- triggers: list of required tokens, stored as JSON array
CREATE TABLE triggers (
  id              INTEGER PRIMARY KEY,
  memory_id       INTEGER NOT NULL REFERENCES memories(id),
  kind            TEXT NOT NULL CHECK (kind IN ('token_subseq', 'path_glob')),
  first_token     TEXT,          -- denormalized for indexed lookup; null for path_glob
  tokens_json     TEXT,          -- JSON array of tokens; null for path_glob
  path_pattern    TEXT           -- fnmatch pattern; null for token_subseq
);

CREATE INDEX idx_triggers_first_token ON triggers (first_token);
```

**Differences from v8:**
- `memories.type` → `memories.kind` with values `block`/`hint` (rename to avoid migration-by-SQL-alias confusion)
- `triggers.tool_name` + `head_joined` + `head_length` → `triggers.kind` + `first_token` + `tokens_json`
- `kind='token_subseq'` replaces v8's `tool_head`
- `path_glob` unchanged

**session_surfaces, session_turns, memory_associations, consolidation_runs, watcher_state tables** — kept as-is. They're about session bookkeeping and Hebbian links, which still apply.

## 5. Error detection

PostToolUse fires retrieval only on error. We need a reliable definition.

**Primary signal: exit code.** For Bash, Claude Code passes the tool result; we check for `<error>` wrapper tag or `Exit code` prefix (already implemented in v1's `_detect_error`).

**Secondary signals** (for tools without clean exit codes):
- `tool_response` string contains error markers: `ERROR:`, `Traceback (most recent call`, `fatal:`, `Usage:` — language-specific but well-known.
- `is_error` field on the PostToolUse payload (set by Claude Code for some tools).

Keep the detection list short and well-documented. Users can override via a config hook (see §7).

**What we explicitly don't try:**
- Semantic error detection on exit 0 (query returns empty when it shouldn't, API returns wrong shape). Not solvable without LLM in hook, outside scope.

## 6. Reinforcement

One metric per memory: `useful_count / surface_count` (Laplace-smoothed).

**Counter bumps:**
- `surface_count` bumps whenever the memory surfaces (either hook).
- `useful_count` bumps when: memory surfaced on tool call X (either hook), and the next tool call with the same first-token in this session returns success.

The "next call same first-token succeeds" logic lives in PostToolUse:
```
on PostToolUse(call Y, exit 0):
  prev_surfaced = memories that surfaced on the previous call with same first-token this session
  bump useful_count on prev_surfaced
```

Simple, cheap, deterministic. No secondary watcher needed.

**Semantic-error caveat:** if Claude runs a wrong SQL query (exit 0, wrong result), then reruns it correctly (also exit 0), our reinforcement signal is wrong — we'd bump useful_count on whatever surfaced at the wrong-query call, even though no memory actually helped. Accepting this as known inaccuracy; the consolidation agent can correct it via LLM judgment nightly.

## 7. Configurable prompts

Watcher and consolidation prompts are user-overridable.

**Lookup order (first match wins):**
1. `$ENGRAM_WATCHER_PROMPT_PATH` (env var, explicit path to markdown file)
2. `~/.claude/tool-engrams/prompts/watcher.md` (user override)
3. `toolengrams/prompts/defaults/watcher.md` (packaged default)

Same pattern for consolidation.

Defaults shipped in the repo at `toolengrams/prompts/defaults/`. The current `prompts/watcher.py` becomes a thin loader.

**Variable interpolation:** support `{project_slug}`, `{existing_memories_summary}` etc. via simple `str.format`. No Jinja — keep the contract boring.

## 8. Migration from v1

Alpha, no users → clean break.

- New DB schema version. Migrations don't convert old → new; a v1 DB is unreadable by v2.
- Provide a one-shot `engram migrate-v1-to-v2` script that reads v1 DB and re-inserts what it can (body + first 3 tokens of old head as new `token_subseq` trigger; old `feedback` → `block`, old `reference` → `hint`). Best-effort, not comprehensive.
- README tells users to nuke and restart for best results.

## 9. What stays from v1

A lot, actually:
- SQLite backend + migration runner
- Watcher (with configurable prompt)
- Consolidation agent (nightly Opus review)
- Hebbian associations table (for co-activation analytics; not a surface track anymore)
- Session bookkeeping (surfaces, turns)
- The `engram` CLI shell (`remember`, `forget`, `recall`, `pin`, `status`, `dashboard`)
- The overall package layout we just landed (cli/, hooks/, formation/, retrieval/, reinforcement/, consolidation/, prompts/)

What changes is concentrated in three files:
- `hooks/pretool.py` — strips to 50 lines (block-only)
- `hooks/post_tool.py` — grows, owns the `hint` retrieval + injection
- `retrieval/rank.py` → `retrieval/match.py` — subsequence match replaces prefix match

Plus a schema migration and a watcher prompt rewrite.

## 10. Implementation order

Ordered smallest-to-largest, so we can ship-and-measure between each step.

1. **Schema migration + trigger rewrite.** New DB shape, subsequence matching. No behavior change yet because hooks still call the old code paths — but internal data model is v2.
2. **Rename types → kinds, rewrite pretool.** `feedback`/`reference` → `block`/`hint`. `hooks/pretool.py` becomes block-only.
3. **Post-failure hint injection.** `hooks/post_tool.py` runs retrieval on error, injects matching hints as `additionalContext`.
4. **Configurable prompts.** Lookup-order config for watcher/consolidation prompts.
5. **Watcher prompt rewrite.** New default prompt that treats CLI-grammar corrections as the primary signal, understands the `block`/`hint` distinction, generates subsequence triggers.
6. **Migration script.** Best-effort v1 → v2 converter.
7. **README rewrite.** Lead with the CLI-discovery pitch. Concrete before/after from dogfooding.

Steps 1–3 are the core behavior change. 4–6 are ergonomics. 7 is positioning. Dogfood after 3.

## 11. Open questions

1. **Should `block` memories also surface on PostToolUse on failure?** If a block memory matched and Claude got past it somehow (e.g. the deny was dismissed by user), should we still inject the context after the call fails? Probably yes — but small case, defer.
2. **Naming: `block` / `hint`, or something else?** `guard` / `tip`? `deny` / `recall`? Bikeshed-worthy. Keeping `block` / `hint` in the doc as placeholder.
3. **First-token bucket for path triggers?** Currently we fetch all path triggers and fnmatch. Fine at low volume; revisit if path-memory corpus grows.
4. **Session-start surfacing of pinned memories.** Killed in the spec above, but might come back as a narrow feature if dogfooding shows lead-in value.
5. **What about PostToolUse on tool *success* where the output contains an error keyword?** (e.g., a shell command that exits 0 but emits "WARNING:".) Probably punt — too heuristic. Revisit if it comes up.

## 12. Explicitly not in v2

- Regex triggers
- Argument-shape triggers ("this flag is wrong")
- Embedding/semantic search
- SessionStart eager inject
- UserPromptSubmit retrieval
- Hebbian co-activation as a surface track (still stored, for analytics only)
- MCP server variant
- Non-Claude-Code harnesses (Cursor, Aider, etc.)

These are all real ideas, but each dilutes the v2 "ship and measure" story. Revisit after dogfooding.
