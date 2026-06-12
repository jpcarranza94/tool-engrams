# ADR-0009 — Target seam: install-time `--target`, one shared DB, canonical delta vocabulary

- **Status:** Accepted
- **Companion to:** ADR-0008 (neutral data home) and the engine seam
  (`toolengrams/engine/`, PR #50) — together the dual-harness foundation.

## Context

ToolEngrams' hook layer, watcher tick, and consolidation collector were
hardwired to Claude Code: its hook payloads, its tool names, its transcript
JSONL, its `~/.claude/projects` layout. Codex (and later harnesses) emit
near-identical hook events with different tool vocabulary, transcript
format, and config surface. The *target* — the hooked harness memories
surface into — must be swappable, and several targets must be able to run
against one DB on one machine.

## Decision

1. **A target adapter package** (`toolengrams/target/`): plain modules in a
   registry behind a `runtime_checkable` Protocol, mirroring the engine
   seam. The claude-code adapter owns everything that was claude-specific:
   tool whitelist, hint extraction entry, failure detection, transcript
   path resolution (payload-first), the transcript parser, the
   consolidation collector, and the hook-command markers.

2. **The hook learns its target from the `--target` flag baked into the
   wired hook command at install time** (`engram pretool --target codex` in
   that harness's own config). Rejected alternatives:
   - *Payload sniffing*: ambiguous by design — codex deliberately mirrors
     claude's hook payload shape — and adds hot-path logic.
   - *A global env var*: cannot express two targets wired at once.
   The flag defaults to claude-code, so pre-seam wiring keeps working.
   **Fail-open is part of the contract**: an unknown `--target` degrades to
   claude-code with a stderr warning (no argparse `choices=` — its exit 2
   would be a *blocking* hook error in Claude Code, precisely in the
   version-skew scenario where degrading matters most).

3. **One shared DB, no per-harness partitioning.** Memories, triggers, and
   scoring stay harness-neutral; adapters normalize tool vocabulary into
   the neutral hint space before any DB query. The only schema change is
   bookkeeping: `watcher_state.target` (v16) tells a detached tick which
   transcript parser to use, and `watcher_runs.engine` attributes each run
   to its engine adapter. `slugify_cwd(cwd)` stays the project-scope key,
   so the same repo shares scope across targets.

4. **Canonical delta vocabulary.** Every target's `format_delta` emits the
   same speaker labels — `USER:` / `AGENT:` / `TOOL (X): …` / `RESULT: …`.
   The assistant label changed from `CLAUDE:` to `AGENT:`; the parser and
   the watcher/eval prompt defaults changed in the same commit. The
   formation gate's literal `TOOL (` / `RESULT:` markers are unchanged and
   now pinned by test — a codex parser that fails to emit them would
   silently disable formation for codex sessions.

5. **install.sh stays the single entry, per-harness scripts do the work.**
   The trunk owns shared steps (python preflight, package install, data-home
   migration, DB init, doctor, schedule, engine persistence to
   `<home>/config.json`) and dispatches `install/targets/<name>.sh` /
   `install/engines/<name>.sh` on the `--target` (repeatable) / `--engine`
   flags. Ordering constraint worth recording: **the data-home migration
   must precede anything that creates the new home** (the engine
   persistence write) — `migrate_legacy_home` only moves the legacy home
   into a not-yet-existing `$DB_DIR`.

## Consequences

- Pre-seam wiring (commands without `--target`) keeps working and the
  installer's marker prefix-match treats it as already-present — installs
  converge without rewriting user settings.
- Mixed versions: NEW wiring (`--target codex`) + OLD package (no flag at
  all) is the one un-degradable skew — the old argparse rejects the flag.
  Acceptable: it requires wiring a target that the installed package
  predates, which the codex target installer's preflight can check.
- The eval/judge flow and scoring need no changes for a second target —
  they operate on DB rows and the canonical delta text.
- Multi-target consolidation collection is deferred (the collector
  interface is per-target; `cli/consolidate` still calls the claude-code
  collector directly until the codex target lands).
