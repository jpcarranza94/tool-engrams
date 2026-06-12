# ADR-0008 — Neutral data home: $ENGRAM_HOME → ~/.tool-engrams, legacy migration via mv + symlink

- **Status:** Accepted
- **Context for:** the dual-harness work (swappable target/engine harnesses;
  Claude Code + Codex)

## Context

All persistent state (sqlite DB, watcher log, sandboxes, pause flag, prompt
overrides) lived under `~/.claude/tool-engrams/`. ToolEngrams is growing
past Claude Code: the hooked harness (target) and the headless background
runner (engine) are both becoming swappable, and Codex is the first second
harness. A codex-only machine installing its memory system into `~/.claude/`
reads as broken, and the path bakes a harness assumption into every
subsystem.

## Decision

One resolution seam, `toolengrams/paths.py::engram_home()`:

1. `$ENGRAM_HOME` — explicit override
2. `~/.tool-engrams` — neutral default, when it already exists
3. `~/.claude/tool-engrams` — legacy home, when it already exists
4. `~/.tool-engrams` — fresh-install default

Everything routes through the seam, directly (`db.db_path()`,
`watcher/log.py::log_path()`, prompt overrides, consolidation schedule log
dir) or transitively via `db.db_path().parent` (pause flag, watcher
sandboxes, cleanup markers). Resolution is call-time wherever a consumer
could observe a change (`db_path`, `log_path`, `_user_override_dir`); the
consolidation schedule deliberately freezes the resolved path into the
launchd plist / cron line because the scheduled job runs with a minimal env.

**Migration** happens once, in install.sh (`migrate_legacy_home()`): when
`$ENGRAM_HOME` is unset, the legacy dir is real, and the neutral default is
absent — `mv` the dir, then leave a symlink at the old path. The symlink
keeps old package versions (hardcoded legacy paths in running sessions,
stale venv binaries, old launchd plists) landing on the same data.

**The code-level legacy fallback (step 3) exists for the reverse skew**: a
package updated ahead of a re-run of install.sh must keep finding existing
memories — without it, updating silently flips users to an empty DB.

## Alternatives considered

- **Reverse symlink** (`ln -s ~/.claude/tool-engrams ~/.tool-engrams`, no
  `mv`): eliminates the move-while-open and partial-failure windows — the
  bytes never move. Rejected because the data physically stays under
  `~/.claude/`, which defeats the point: a user decommissioning Claude Code
  (`rm -rf ~/.claude`) would delete their harness-neutral memory corpus.
- **Env-only override, no migration**: leaves every existing install on the
  legacy path forever; the neutral home would exist only in docs.
- **No code-level fallback (install-time migration only)**: breaks the
  update-package-first ordering, which is the common case for a git-pull
  user.

## Consequences

- `engram doctor` reports the resolved home; WARNs on the legacy location
  (nudge to re-run install.sh) and on split-brain (a real legacy dir
  coexisting with another resolved home — old package versions write there).
- Both-exist is never auto-merged: install.sh warns and uses the neutral
  home; merging diverged sqlite files is a human decision.
- `$ENGRAM_HOME` is a machine-wide contract: set it everywhere (shell,
  launchd) or nowhere. install.sh warns when it's set while legacy data
  exists, and skips migration — it can't know the override's intent.
- A stray `mkdir ~/.tool-engrams` flips resolution away from a populated
  legacy dir (step 2 beats step 3). Accepted: doctor's split-brain WARN
  catches it, and the alternative (preferring legacy) would make the
  migration itself unstable.
- Uninstall keeps the DB and therefore keeps the compatibility symlink;
  full cleanup (README) removes both.
