# Changelog

All notable changes to ToolEngrams. The project is alpha — breaking changes
land on `main` without deprecation cycles; pin a tag if you need stability.

## [Unreleased]

### Changed
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
