# ADR-0004 — Claude Code plugin packaging: built, then rejected

- **Status:** Rejected (implemented in PR #37, removed the next day)
- **Context doc:** `docs/prd-open-source-release.md` Phase 2

## Context

The PRD's Phase 2 packaged ToolEngrams as a Claude Code plugin: manifests in
`.claude-plugin/`, all 8 hooks routed through a fail-open shell shim, a
detached venv bootstrap under `${CLAUDE_PLUGIN_DATA}` (the plugin system has
no postinstall hook), skills renamed for namespace-friendly invocations, and
mutual-exclusivity guards against the legacy `install.sh` path. It shipped in
PR #37, fully tested (shim unit tests + ubuntu:24.04 end-to-end runs).

## Decision

Remove the plugin path and make `install.sh` the single install method,
investing in its DX instead (PRD Phase 3).

What tipped it, with the implementation in hand rather than on paper:

1. **The bootstrap was all workaround, no feature.** No postinstall hook means
   a ~130-line shim/venv/stamp apparatus with real failure modes we had to
   harden one by one (exec-during-rebuild race, stale locks, invisible
   permanent failures, log growth). install.sh needs none of it.
2. **Dark windows.** The plugin was dark for the first session and during
   every update rebuild. For a system whose pitch is "memories surface
   automatically," a silent first session is the worst possible first
   impression. install.sh works the moment it exits.
3. **Two contracts forever.** Every hook change had to land in both
   plugin.json and install.sh; a tripwire test caught drift but the
   maintenance tax stays.
4. **Debuggability.** Script path: hooks visible in settings.json, `engram`
   on PATH, failures in the terminal. Plugin path: shim indirection, a venv
   buried in plugin data, failures in a background log.
5. **The author can't use it.** The plugin venv install is non-editable —
   every local change needs a stamp-busting rebuild — so the developer
   install is `install.sh -e` regardless, leaving the plugin path
   second-class and undertested in daily use.

## What survives from the Phase 2 work

- `install.sh --uninstall` (marker-based hook removal, keeps the DB) and the
  unknown-flag guard.
- `utils.prepend_engram_bin` — the watcher/consolidation `claude -p` children
  get the interpreter's bin dir on PATH, which the install.sh **venv
  fallback** (PEP 668 machines) needs just as much as the plugin did.
- The verified plugin-system facts above, and the shim/bootstrap design,
  preserved in git history (`1650f3a`) if the plugin path is ever revisited —
  revisit if Claude Code ships a postinstall/bootstrap hook, which removes
  objection 1 and most of 2.

## Decision preserved from the plugin work: DB location

The Phase 2 analysis concluded memories are **user data**: had the plugin
shipped, the DB would still live at `~/.claude/tool-engrams/db.sqlite`, never
inside plugin-managed storage (where uninstall deletes it). That reasoning
stands independent of packaging and applies to any future re-packaging.
