# Changelog

All notable changes to ToolEngrams. The project is alpha — breaking changes
land on `main` without deprecation cycles; pin a tag if you need stability.

## [Unreleased]

### Fixed
- **`q` quality ratio derives from `session_surfaces` ground truth (ADR-0013).**
  `useful_count`/`noise_count` had drifted below memories' real helpfulness —
  the v12 migration zeroed `useful_count`, `restore` zeroed it again (while
  `archive` never touched it), and `judge` bumped +1 per call while closing N
  surface rows. A consolidation run found 18 active memories gate-suppressed
  (`q<0.5`) despite being net-helpful by actual surfaces (one: 37 helpful/6
  noise → `q=0.36`). Counters are now a cached projection of `session_surfaces`:
  `judge` bumps by rows-closed, `restore` recomputes, and the new
  **`engram rebuild-counters`** one-shot heals existing drift (`--dry-run` to
  preview). Run it once to re-surface the suppressed memories.

### Added
- **Tuning knobs moved into config.json (ADR-0012).** The behavior constants
  worth tuning are now config-backed via call-time resolvers (`env_int`/
  `env_float`, read after `hydrate_env`): the q surfacing gate
  (`gate.threshold`, `gate.warmup_n`), the formation
  `formation.similarity_threshold`, consolidation
  `consolidation.{catchup_lookback_days,surfaces_ttl_days,watcher_runs_ttl_days,max_sessions,timeout}`,
  and `watcher.max_form_retries`. Each keeps its module constant as the default;
  set via `engram config set <key> <value>`. Structural invariants (sweep-spawn
  cap, sentinels, unit constants, schema version) are deliberately left as named
  constants, not config — they're correctness constraints, not preferences.
- **Formation near-duplicate gate (ADR-0014).** `engram remember` now surfaces
  the top-3 textually-similar existing memories (FTS5 shortlist re-scored by
  token-Jaccard — no new dependency) and, on a strong match, returns
  `action: "review_similar"` *without inserting* instead of creating a same-idea
  duplicate that trigger-overlap dedup misses. The (remember-only) formation
  agent then folds in with `engram remember --into <id>` or insists with
  `--force`. Catches the different-trigger/same-idea dupes (e.g. the three
  `macos-no-timeout-command` rows) at the source.
- **Durable config file + `engram config` / `engram engine` verbs (ADR-0012).**
  `<engram home>/config.json` grows from a flat `engine` key into an
  engine-keyed schema covering the active engine, per-engine models, watcher
  tuning, and prompt overrides. `engram engine set codex` switches the
  background engine with no reinstall (next tick picks it up); `engram config
  set engines.codex.eval_model gpt-5` sets a per-engine model;
  `engram config show` lists every key with its effective value and source.
  The file is projected into `os.environ` (`config.hydrate_env()`) for any
  `ENGRAM_*` not already set, so precedence is **explicit env > file > default**
  and every existing `os.environ.get` call site is untouched. JSON (stdlib) was
  chosen over YAML/TOML to keep the hot path dependency-free. The *target*
  harness stays install-time wired (several coexist); only the *engine* is a
  runtime switch.

### Changed
- **Consolidation is a catch-up sweep (ADR-0011).** The scheduled
  `--yesterday` run now consolidates every un-run day in the last 7 days
  (oldest-first), not just `today - 1`. A day missed because the laptop was off
  when the 8 AM job would fire is backfilled on the next run —
  `consolidation_runs` is the coverage source of truth, so done days are
  skipped. Empty days are no longer recorded (cheap to rescan, no run-history
  pollution); errored days are no longer recorded either, so a transient
  spawn/timeout/PATH failure is retried next run instead of permanently skipping
  a day. `RunAtLoad` flips to `true` so the idempotent sweep drains backlog on
  boot; a non-blocking `flock` (`consolidate.lock`) keeps the boot fire from
  overlapping the 8 AM fire and double-spending an Opus call on the same day, and
  connections now set `PRAGMA busy_timeout` so a contended writer waits instead
  of failing fast. `--yesterday --json` output is now a
  `{status, surfaces_cleaned, runs: [...]}` aggregate. Re-run
  `engram consolidate --install-schedule` to pick up `RunAtLoad`. (Linux cron
  has no `@reboot` parity — macOS only for now.)
- **Stateless watcher ticks (ADR-0005).** Every formation/eval tick is now a
  fresh `claude -p` call — the resumed-conversation design is gone. A
  5-session transcript audit found conversation state changed the outcome in
  ~5% of formation ticks and ~1% of eval judgments while costing a 2.5–9×
  premium (up to 96% of spend was re-carrying history past the cache TTL).
  The two useful bits of state are re-supplied explicitly: formation gets the
  prior-window tail (≤4k chars, re-read via run-log cursor spans) and the
  list of memories it already saved this session; `engram remember` echoes
  the body it replaces on dedup with a merge instruction. Eval needs nothing —
  the re-presented pending list was always its real state. Drops the resume
  failure class (orphaned ids, 200k-context runway, timeout-amplifying
  re-reads); `watcher_state` loses `watcher_session_id`/`watcher_pid` (v15).
- **Same-session suppression (ADR-0006).** A hint never surfaces into the
  work session that formed it (`memories.origin_session_id`, set via
  `ENGRAM_ORIGIN_SESSION` in the watcher child env or `--origin-session`).
  Same-session "helpful" was self-confirmation, not transfer — `q` now
  measures what it claims. Blocks exempt (enforcement fires where the lesson
  was learned); manual saves have NULL origin and are never suppressed.
  Forward-only: pre-existing rows have no origin recorded.
- `memory_store.add_token_trigger` now derives `first_token` itself
  (signature: `(conn, memory_id, tokens)`) and rejects empty token lists.
  Settles the casing contract in one place: stored as-is, matched
  case-sensitively (command names are case-sensitive); the schema comment
  claiming lowercasing was stale and is corrected. No behavioral change
  for existing data — no writer ever lowercased.
- e2e `seed_memory` fixture now writes through `memory_store` (the
  documented seam) instead of raw SQL, so fixture/schema drift is caught
  by unit tests instead of a paid `claude -p` run. The v1
  `tool_head`/`head` trigger shape and its conftest shim are gone — e2e
  tests author `token_subseq`/`path_glob` directly.

### Added
- **`engram edit` (ADR-0007)** — in-place body/name/description correction
  preserving id, counters, surfaces, and triggers; stamps `last_verified_ts`;
  `--re-extract-triggers` opt-in. Ends the destructive forget-and-re-remember
  dance. Interactive + consolidation tier only.
- **`engram quarantine <id> --reason` (ADR-0007)** — the eval watcher's
  emergency brake for demonstrably harmful memories: archives the memory
  (out of retrieval immediately; restorable via `engram forget --restore`),
  records an audited `quarantined` run-event with the reason, and
  noise-marks unjudged surfaces. Id-only, no bulk, no hard delete; added to
  the eval allowlist alongside `engram judge`. Nightly consolidation
  receives the quarantine list and must restore, repair (`engram edit`),
  or confirm each.

### Fixed
- e2e suite runs again: the fixtures still wrote the v1 `memories.type`
  column (dropped in the v2 schema), so all 7 `claude -p` tests failed at
  seed time. Fixtures now use `kind` (`hint`/`block`). The run confirms
  hint delivery still works after the 0.1.0 security fix (the
  no-`permissionDecision` contract itself is pinned by unit tests —
  e2e can't observe a hook's permission output, only its effects).

## [0.1.0] — 2026-06-10

First tagged release. Everything before this point — the system itself.

### Core
- Tool-bound memories (`hint` / `block`) surfaced via Claude Code hooks:
  PreToolUse (block deny + hint context), PostToolUse (turn counter,
  recovery tick), PostToolUseFailure (hint on real failures).
- Subsequence trigger matching (`token_subseq`, gaps allowed) + `path_glob`
  bindings; single indexed SQL lookup on the hot path (stdlib + sqlite3 only).
- Background watcher (formation + evaluation roles) as permissioned
  `claude -p` sessions gated by a command allowlist; noise-aware scoring
  (`q`) with a surfacing gate; nightly Opus consolidation with
  trigger-narrowing.
- Kill switch: `engram pause` / `resume` / `ENGRAM_DISABLED`.

### Security
- **Hints no longer emit a `permissionDecision`.** Previously a hint-only
  match emitted `allow`, which silently bypassed Claude Code's permission
  prompts for any command an autonomously-formed trigger matched. Hints now
  inject `additionalContext` only; the permission flow is untouched. Only
  `block` emits a decision (`deny`).
- Secrets gate on `engram remember`; per-role watcher command allowlists.

### First-run experience
- `engram doctor`: wiring + liveness diagnostics (hooks, PATH, claude
  version, DB, last hook fire / watcher tick), exit 1 on failure.
- `engram seed` is hint-only by default (`--with-block` opts into a deny
  demo; `--remove` cleans up, including surface rows); legacy block-kind
  seeds are realigned in place.
- README quickstart + "Verify it's working" walkthrough;
  `ENGRAM_SURFACE_NOTICE=1` makes surfaces visible in the transcript.
- `engram status`: human summary on a tty, JSON when piped.
- install.sh: PEP 668 fallback chain, tty-gated schedule prompt (headless
  installs work), per-hook uninstall surgery, loud PATH warning when the
  venv fallback links into a `~/.local/bin` that isn't on PATH.
- Removed the unimplemented `engram export` stub from the CLI.

### CI
- GitHub Actions: unit matrix (Python 3.10 / 3.13) + headless install.sh
  smoke test on Linux and macOS; Dependabot for actions; the four checks
  are required on `main`.
