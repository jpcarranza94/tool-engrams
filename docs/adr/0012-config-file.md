# ADR-0012 — One config file (config.json) projected into the environment, env still wins

- **Status:** Accepted
- **Context for:** switching the engine and tuning per-engine models without a
  reinstall, now that the engine/target seams (#51/#52) and the Codex adapters
  (#53/#54) have multiplied the `ENGRAM_*` surface

## Context

Durable settings were split across two mechanisms. The **active engine** lived
in `<engram home>/config.json` as a single flat `engine` key (written by
install.sh, read by `engine/selection.py`). **Everything else** — per-engine
models (`ENGRAM_WATCHER_MODEL` vs `ENGRAM_CODEX_WATCHER_MODEL`, plus formation/
eval), background tuning (`TICK_COALESCE_SEC`, `IDLE_SWEEP_SEC`,
`WATCHER_TIMEOUT`, `MAX_MEMORIES_PER_CALL`, …), and prompt overrides — was
loose env vars only. To change a model or switch engines durably you re-ran
install.sh or hand-exported vars into launchd, neither of which is a "switch
without reinstall."

The codebase already reads each setting via `os.environ.get("ENGRAM_*")` at the
point of use, deliberately, so the values survive launchd/cron's minimal env.
Any config file has to feed *that*, not replace it.

## Decision

Extend the existing `config.json` into a nested, engine-keyed schema and add one
projection step. `toolengrams/config.py` owns a `SPEC` mapping every dotted key
to its env var and value type, and `hydrate_env()` writes each file value into
`os.environ` **only for vars not already set**. Precedence is therefore:

    explicit env  >  config file  >  built-in default

`hydrate_env()` is called once per process in `__main__.main()` before dispatch,
for **every** command except `config`/`engine` (which must compare env against
file). Because the projection lands in `os.environ`, **no existing
`os.environ.get` call site changes** — the file is transparent to the watcher,
the engine adapters, the surfacing hooks, and the prompt loader alike.

Two CLI verbs write/read the file: `engram config show|get|set|unset|keys` and
`engram engine show|set|list`. `set` validates the key against `SPEC` (a typo
errors instead of silently no-op'ing) and coerces the value to the declared
type; `engine set` validates against the engine registry and warns — but does
not fail — when the binary is absent (background selection is fail-open).

**Format: JSON, not YAML/TOML.** The hot path (surfacing hooks) reads
`MAX_MEMORIES_PER_CALL` / `SURFACE_NOTICE`, so `hydrate_env()` runs there too —
it must stay stdlib. `json` is stdlib on every supported version; `tomllib` is
3.11+ (the floor is 3.10) and YAML needs a third-party parser, so both would add
the project's first non-`rich` dependency for no ergonomic win that a
documented schema plus `engram config` doesn't already provide.

**Engine is switchable; target is not.** The engine is a pure runtime selection,
so the file (or `engram engine set`) flips it for the next detached tick. The
target is hooks physically wired into the harness's own config; a file cannot
wire/unwire them. But several targets can be wired at once, so the answer is to
wire both and let them coexist — there is nothing to "switch."

## Alternatives considered

- **Keep the flat `engine` key + loose env vars:** the status quo; no durable,
  discoverable home for per-engine models or tuning, and no switch-without-
  reinstall.
- **YAML (`PyYAML`) / TOML (`tomli` backport):** nicer nesting/comments, but a
  new dependency that would also be imported on the hot path. Rejected — JSON is
  stdlib and the `engram config` verbs cover the ergonomics.
- **A config object threaded through call sites instead of env projection:**
  touches every `os.environ.get` consumer and breaks the minimal-env survival
  property that detached launchd/cron ticks rely on. The env is already the
  contract; the file should feed it, not replace it.
- **Hydrate inside `selection.get_engine()` / each `resolve_model()` only:**
  misses the hot-path surfacing knobs and the prompt loader; a single
  entrypoint projection is simpler and uniform.

## Consequences

- `config show` reports, per key, the file value, any env override, and the
  effective value with its source — so "why is the watcher on sonnet?" is one
  command.
- A malformed `config.json` is fail-open (`load()` returns `{}`): a broken file
  never breaks a hook or a tick; it just falls back to env/defaults.
- The hot path gains one small stdlib JSON read per process. No new dependency,
  no network, no LLM — the "single-digit-ms SQL" character of the hooks holds.
- install.sh's flat `engine` write stays forward-compatible: it loads the
  existing file and sets one key, preserving any nested blocks.
- Internal per-process vars (`ENGRAM_IN_WATCHER`, `ENGRAM_RUN_ID`,
  `ENGRAM_ALLOWED_VERBS`, the `ENGRAM_DISABLED` pause flag) and bootstrap paths
  (`ENGRAM_HOME` / `ENGRAM_DB`) are intentionally not in `SPEC` — the file lives
  under the home, so the home cannot be configured by it, and the watcher-
  containment vars must be set by the parent, never by a user-editable file.
