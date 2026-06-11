# PRD — Open-source release & Claude Code plugin packaging

- **Status:** Draft
- **Date:** 2026-06-09
- **Supersedes:** `TODO-open-source.md` (deleted; unfinished items folded into Phase 1/4 below)
- **Inputs:** four independent audits run 2026-06-09 — OSS-readiness, UX/DX journey, Claude Code plugin-system capabilities, and a structural study of the `caveman` plugin (github.com/JuliusBrussee/caveman) as the packaging reference.

## Problem

ToolEngrams works (memories surface at PreToolUse, watchers form and judge them, the
monitor shows live spend) but it is installable only by its author. The install path
assumes a configured dev machine, the repo still carries employer artifacts, there is
no off switch for a system that denies tool calls and spends money in the background,
and the primary "save this" skill is broken against the current CLI. Meanwhile Claude
Code now has a first-class plugin system that solves the two ugliest parts of our
installer (settings.json surgery, skill symlinks) natively.

## Goals

1. A stranger on a fresh macOS or Linux machine reaches a working install — and *sees
   a memory fire* — in under 5 minutes.
2. ~~Install UX is two slash commands via the plugin system; `install.sh` remains as the
   non-plugin fallback.~~ *Superseded: the plugin packaging was built and rejected
   (`docs/adr/0004`); `install.sh` is the single install path, with Phase 3 investing
   in its DX.*
3. The autonomy is governable: one command pauses everything; cost and privacy are
   documented before the first dollar is spent.
4. The repo is presentable: no employer strings, no stale docs, no broken skills.

## Non-goals

- PyPI distribution (editable install only for the alpha).
- Windows support.
- Multi-user / team memory sharing.
- Renaming the project or the core concepts (keep: engram, hint, block, trigger, pin).

## Current state (verdict: nearly-ready)

Publish-quality already: MIT license, clean `pyproject.toml` + entry point, 483
portable tests (~1.3s, no network/HOME deps), merge-don't-clobber idempotent settings
handling, cross-platform consolidation scheduling (launchd + cron), secrets gate on
memory formation, `engram monitor` with per-run USD cost, fail-open hooks throughout.

What blocks release is the last mile, phased below. Each item carries its audit
source: **[OSS]** readiness audit, **[UX]** UX/DX audit, **[PLG]** plugin research,
**[CAV]** caveman study.

---

## Phase 1 — Release blockers (correctness, safety, hygiene)

### 1.1 Fix the `engram-remember` skill **[UX — worst single defect]**
`skills/engram-remember/SKILL.md` documents `--type feedback|reference`; the CLI only
accepts `--kind block|hint` (`cli/remember.py`). Every skill-driven save errors.
**Do:** audit all three SKILL.md files against the current argparse surface; add a test
that greps skill docs for flags and asserts they exist in the parser (cheap drift guard).
**Accept:** `/engram-remember` produces a working `engram remember` call first try.

### 1.2 Kill switch **[UX — trust grade D+ driver]**
No way to stop the system short of hand-editing `~/.claude/settings.json`. A tool that
auto-denies tool calls and spawns paid `claude -p` processes needs a one-command off.
**Do:** `engram pause` / `engram resume` toggle a flag file next to the DB — the
primary mechanism, checked at the top of every hook entry point and `watcher/tick.py`
(fail-open exit). `ENGRAM_DISABLED=1` is honored in the same check as a scripting/CI
override; precedence: env var beats flag file. `engram doctor` reports both. Document
on the README's first screen.
**Accept:** `engram pause` → no surfacing, no ticks, no spend; `engram resume` restores.

### 1.3 Fresh-machine installer **[OSS — blockers 1–3]**
- `install.sh` checks `pip`, not `pip3` → fails on stock macOS/Debian.
- No PEP 668 handling → Homebrew/Debian Python rejects bare `pip install -e`, and
  `2>&1 | tail -1` under `set -e` swallows the real error. Fall back to
  `--user` / venv / pipx with a clear message.
- The `uv pip install --system` branch (tried before pip) has its own managed-Python
  failure modes — same treatment: clear error, documented fallback.
- `engram status >/dev/null 2>&1` dies silently (exit 127, output swallowed) when
  `engram` is not on PATH after a `--user` install.
- No version checks: enforce Python ≥ 3.10 and `claude` ≥ 2.1.117 up front with
  actionable errors (README documents both; the script checks neither).
- Back up `settings.json` to `.bak` before the first write **[CAV convention]**.
**Accept:** clean install on a stock macOS VM and an Ubuntu container, or a correct,
actionable error.

### 1.4 De-employer the repo **[OSS — blocker 4]**
`jenkins.ergeon.in` (`retrieval/extract.py:89-90`, `tests/test_extract_compound_expansion.py`),
`jira.ergeon.in` (`tests/test_trigger_validation.py`), `ergeon` examples
(README, `retrieval/rank.py:73` docstring, `tests/test_pretool.py`, `tests/test_rank.py`,
`migrations/v6.sql`, `tests/test_hooks_skip.py`), `/Users/jpcar` literals (3 test files:
`test_collect_sessions.py`, `test_hooks_skip.py`, `test_session_start.py`, plus
`toolengrams/utils.py` + `cli/resolve_slug.py` docstrings). Replace with generic
equivalents (`jenkins.example.com`, `mycli`, `/Users/dev`).
**Accept:** `git grep -iE 'ergeon|/Users/jpcar' -- ':!docs/prd-open-source-release.md'`
returns nothing. (`jpcarranza94` is the repo handle and stays — clone URLs and the
plugin marketplace command legitimately contain it; this PRD's own receipts are
excluded from the gate.)

### 1.5 True up the docs **[OSS — blocker 5]**
`CLAUDE.md`: "133 unit tests" → drop the count (the model/timeout defaults were
already trued up by PR #34). README: drop the "~420 tests" number. Delete
`TODO-open-source.md` (done in this PRD's own PR).
**Accept:** no test-count literals anywhere; doc numbers that can drift are removed,
not refreshed.

### 1.6 Cost & privacy section in README **[UX + OSS]**
The biggest undisclosed surprise. State: what runs in the background (one sonnet
`claude -p` per coalesced Stop event per role), spend (~$1/day moderate use [superseded: README now states $1-$9/day from sonnet data] —
preliminary, one day of mixed-model data; see open question 4; eval ≈ 5× formation
per call), the levers (`ENGRAM_WATCHER_MODEL=haiku`, per-role
overrides, `engram pause`), live visibility (`engram monitor`), what is stored where
(transcript deltas in sandbox cwds, excerpts in the DB, 7-day residue TTL), and the
secrets gate.
**Accept:** a reader knows the daily cost and the data flows before installing.

### 1.7 Threat-model paragraph **[review of this PRD]**
The system injects memory bodies into Claude's context at PreToolUse and can DENY
tool calls; memories form autonomously from transcripts via a background LLM. That
makes poisoned / prompt-injecting memory content a real attack surface for a public
release. **Do:** a short SECURITY section in the README covering: memory bodies are
untrusted input to future sessions (the `block` deny text especially); the watcher's
per-role `claude -p` command allowlist (`Bash(engram remember *)` / `Bash(engram
judge *)`) as the containment boundary; the secrets gate; and how to audit what
formed (`engram recall`, `engram monitor` decision stream).
**Accept:** SECURITY section exists; the allowlist boundary is documented.

---

## Phase 2 — Plugin packaging **[PLG + CAV]** — *implemented in PR #37, then REJECTED; see `docs/adr/0004`. install.sh is the single install path; goal 2's "two slash commands" is superseded by investing in install.sh DX (Phase 3).*

The plugin system supports everything we need: all 8 hook events, skills, agents.
Target install UX:

```
/plugin marketplace add jpcarranza94/tool-engrams
/plugin install tool-engrams@tool-engrams
```

### 2.1 Repo layout
```
.claude-plugin/
├── plugin.json          # name, version (EXPLICIT — caveman omits it and gets opaque SHA versions),
│                        # hooks (all 8, ${CLAUDE_PLUGIN_ROOT} paths, timeout + statusMessage on each)
└── marketplace.json     # source: "./" — the repo is its own marketplace
skills/<name>/SKILL.md   # discovered by convention; replaces the symlink scheme
```
Skills namespace as `/tool-engrams:<name>` — shorten skill names to `remember` /
`recall` / `forget` since the namespace already disambiguates.

### 2.2 Python bootstrap (the one real gap: no postinstall hook)
SessionStart hook bootstraps a plugin-scoped venv, the documented pattern:
stamp-compare `pyproject.toml` against a copy in `${CLAUDE_PLUGIN_DATA}`; on mismatch,
`python3 -m venv ${CLAUDE_PLUGIN_DATA}/venv && pip install ${CLAUDE_PLUGIN_ROOT}`.
All hook commands invoke `${CLAUDE_PLUGIN_DATA}/venv/bin/engram …`.

The first-run install takes tens of seconds and a SessionStart hook has a single-digit
timeout budget — the bootstrap must NOT run inline. The stamp check stays in-hook
(one diff, ms); on mismatch it spawns the venv build **detached** (the
`spawn_tick` pattern), sets a `statusMessage`, and exits 0. Hooks fail open while the
venv is absent (memory system dark for that first session, live from the next one);
`engram doctor` and the SessionStart context both surface "bootstrap in progress".

### 2.3 DB location
`${CLAUDE_PLUGIN_DATA}/db.sqlite` (`~/.claude/plugins/data/tool-engrams/`) — survives
plugin updates, removed on uninstall (with prompt). Source: the official plugin
reference (code.claude.com/docs/en/plugins-reference.md) documents
`${CLAUDE_PLUGIN_DATA}` and its persistence/uninstall semantics — **verify against
live docs at Phase 2 start before building on it**, since both 2.2 and 2.3 hinge on
it. `$ENGRAM_DB` already makes this a one-line change; keep `~/.claude/tool-engrams/`
for the legacy install path and document a migration note (`mv` the DB or set
`$ENGRAM_DB`).

### 2.4 What NOT to copy from caveman
Its installer *also* copies hooks into `~/.claude/hooks/` and edits settings.json with
absolute paths — a legacy cross-agent artifact that double-fires hooks. Plugin path and
`install.sh` path must be mutually exclusive and documented as such (install.sh gains
a check: if the plugin is installed, refuse).

Migration for existing `install.sh` users goes the other way too: an
`install.sh --uninstall` (or `engram doctor --fix`) that removes the 8 settings.json
hook entries (marker-based, like the install) and the skill symlinks, so switching to
the plugin is: uninstall script → install plugin → point `$ENGRAM_DB` (or `mv` the DB).

### 2.5 Record the decision
Write `docs/adr/0004-plugin-packaging.md` when implemented (DB under
`CLAUDE_PLUGIN_DATA`, venv bootstrap via SessionStart, plugin-vs-script exclusivity).

**Accept:** two-command install on a machine that has never seen the repo; uninstall
via `/plugin uninstall` leaves no hooks behind; legacy `install.sh` still works and
refuses to double-install.

---

## Phase 3 — DX polish **[UX wins 3–5]**

- **3.1 Quickstart at the top of README:** install → `engram seed` → "ask Claude to
  force-push; watch the block fire" with a pasted transcript or GIF. Architecture
  deep-dive moves below the fold / into `docs/design.md`.
- **3.2 Humanize the CLI:** `engram recall`/`status` print readable tables on TTY,
  JSON when piped (reuse `monitor.py`'s pattern); group `engram --help` into Memory /
  Inspection / Internals; hide hook plumbing + one-shot migrations from the default
  listing; delete the `export` stub.
- **3.3 Surfaced-hint footer:** append `(matched: <trigger> · unhelpful? engram skip
  "<name>")` to injected hints — answers "why did this appear" and "how do I push
  back" in one line.
- **3.4 `engram doctor`:** hooks wired? `claude` on PATH and ≥ min version? DB
  writable? watcher ran recently? Prints one line per check.

---

## Phase 4 — Repo trust signals (carried from TODO-open-source.md)

CONTRIBUTING.md (20 lines), GitHub Actions CI (pytest on push) + badges, issue
templates, `CHANGELOG.md` + `v0.1.0` tag, `py.typed`, author contact in
`pyproject.toml`, `.gitignore` additions (`*.log`, `.coverage`, `.idea/`, `.vscode/`,
`.eggs/`, `*.egg`, `htmlcov/`).

---

## Sequencing & rough effort

| Phase | Effort | Gate |
|---|---|---|
| 1 — blockers | ~1 day | nothing publishes before this |
| 2 — plugin | ~1 day | built, then rejected — see `docs/adr/0004` |
| 3 — DX polish | ~½ day | pre-announcement |
| 4 — trust signals | ~½ day | pre-announcement, parallelizable with 3 |

## Open questions

1. ~~Plugin name: `tool-engrams` vs `engrams`.~~ *Moot — plugin rejected
   (`docs/adr/0004`).*
2. Keep `version` unset during alpha (every commit = update, caveman-style) or semver
   from day one? Leaning: explicit `0.1.0` at announcement, unset until then.
3. Does the marketplace listing need `defaultEnabled: false` given the system spends
   money once enabled? Leaning yes + first-run cost notice via SessionStart context.
4. The 1.6 "~$1/day" figure is one day of mixed opus/sonnet data — preliminary. (Superseded: README's Cost section now carries an observed $1-$9/day sonnet range.)
   Collect a week of sonnet-only `engram monitor` data before the README cost table
   is presented as measured fact (gate on this only for the announcement, not the
   repo going public).
