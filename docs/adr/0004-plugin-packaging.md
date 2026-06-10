# ADR-0004 — Claude Code plugin packaging

- **Status:** Accepted
- **Context doc:** `docs/prd-open-source-release.md` Phase 2

## Context

The legacy installer (`install.sh`) does settings.json surgery and skill symlinks
by hand. Claude Code's plugin system does both natively: hooks declared in
`plugin.json`, skills discovered from `skills/<name>/SKILL.md`, a repo can be its
own marketplace. The one gap: there is no postinstall hook, and the package needs
a Python environment.

Facts verified against the official docs (code.claude.com/docs, 2026-06-09):
all 8 hook events we use are supported in plugins; hook `timeout` is in
**seconds**; `${CLAUDE_PLUGIN_ROOT}` points at the installed plugin (path changes
per update); `${CLAUDE_PLUGIN_DATA}` resolves to `~/.claude/plugins/data/<id>/`,
survives updates, and is deleted on uninstall from the last scope (with prompt;
CLI honors `--keep-data`); `defaultEnabled: false` exists (v2.1.154+).

## Decisions

### 1. Venv bootstrap via a fail-open hook shim, never inline

Every plugin hook routes through `plugin/hook.sh <ROOT> <DATA> <subcommand>`:

- It stamp-compares `$DATA/install.stamp` against (pyproject.toml content +
  the plugin root path). The root path changes on every plugin update, so
  updates trigger a rebuild even when pyproject.toml didn't change.
- On mismatch it spawns `plugin/bootstrap.sh` **detached** (the build takes tens
  of seconds; hooks have second-scale budgets) and fails open — `{}` output, or
  a "bootstrap in progress" `additionalContext` on SessionStart. The memory
  system is dark for the first session, live from the next.
- When the venv exists it `exec`s `$DATA/venv/bin/engram <subcommand>`.

`bootstrap.sh` is serialized by a lock dir (15-min stale reap), builds with
`python3 -m venv --clear`, installs the plugin root (non-editable — the root is
a versioned cache path), and writes the stamp **last** so a failed build retries.

### 2. The DB stays at `~/.claude/tool-engrams/db.sqlite` (deviation from PRD 2.3)

The PRD proposed `${CLAUDE_PLUGIN_DATA}/db.sqlite`. Rejected:

- Memories are **user data**, not plugin internals. `/plugin uninstall` deletes
  the data dir — users would lose their memory store by uninstalling the
  packaging around it.
- The store must look the same from every entry point: plugin hooks, legacy
  hooks, skills running `engram` in the user's session, and the user's own
  terminal. A plugin-private DB path forks the view unless every entry point
  threads `$ENGRAM_DB`, which the terminal and skills can't do reliably.
- Switching install methods (script ↔ plugin) needs zero migration.

What *does* live in `${CLAUDE_PLUGIN_DATA}`: the venv, the install stamp, the
bootstrap log — true plugin internals, correctly reaped on uninstall.
`$ENGRAM_DB` still overrides for tests and exotic setups.

### 3. `engram` reaches PATH via `~/.local/bin`; child agents get the venv bin dir

The venv is private, but skills and the user's shell call plain `engram` —
bootstrap symlinks `$DATA/venv/bin/engram` into `~/.local/bin`. The watcher and
consolidation agents spawn `claude -p` children whose allowlists are `engram`
verbs; `utils.prepend_engram_bin` prepends `dirname(sys.executable)` to the
child PATH so the verb resolves under any install method.

### 4. Plugin and script installs are mutually exclusive

Both wire the same 8 hooks; running both double-fires everything (the caveman
plugin's documented failure mode). `install.sh` refuses to run when
`enabledPlugins` contains an enabled `tool-engrams@…`; `install.sh --uninstall`
removes the script-installed hooks (marker: commands starting `engram `), the
`Bash(engram *)` permission, and the skill symlinks — keeping the DB — so the
migration to the plugin is: `./install.sh --uninstall` → `/plugin install`.

### 5. Skill naming: short folders, prefixed legacy symlinks

Folders are `skills/remember|recall|forget` so plugin invocations are
`/tool-engrams:remember` (the namespace already disambiguates). SKILL.md carries
**no** frontmatter `name` — it would override the folder name in both install
paths. The legacy symlinks keep their `engram-` prefix (`/engram-remember`), so
un-namespaced skills can't collide with built-ins like `/remember`.

### 6. `defaultEnabled: false`, explicit version

The system spends money once enabled; the marketplace entry and plugin.json both
ship `defaultEnabled: false` so installing is consent to code, enabling is
consent to spend. `version` is explicit (`0.1.0`) — an omitted version surfaces
as an opaque commit SHA in the plugin UI.

## Consequences

- Two-command install; uninstall leaves no hooks behind; memories survive
  uninstall by design (decision 2).
- First session after install/update runs without the memory system (decision 1
  trade-off). `engram doctor` (Phase 3) should surface bootstrap state.
- `~/.local/bin` must be on PATH for skills/terminal use — true by default on
  Ubuntu and documented for macOS.
- The legacy `install.sh` path remains fully supported for non-plugin setups.
