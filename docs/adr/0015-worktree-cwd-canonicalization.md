# ADR-0015 — Project-scope canonicalizes a worktree cwd to its main repo

- **Status:** Accepted
- **Context for:** a nightly consolidation recommendation (`worktree-cwd memory
  scoping` / `worktree-scoped project memories`) flagging ~37 memories stranded
  on ephemeral worktree paths.

## Context

A `scope=project` memory binds to `slugify_cwd(cwd)` and surfaces only when the
current cwd's slug matches **exactly** (rank.py: `scope='global' OR project_slug
= ?` — no parent-prefix match). Both the formation slug (`remember.py:
_resolve_project_slug`) and the match slug (`pretool.py`) were `slugify_cwd(raw
cwd)`.

When work happens inside a **git worktree**, that cwd is ephemeral:

- a Claude Code harness worktree at `<repo>/.claude/worktrees/agent-<id>`
  (Agent `isolation:'worktree'`), or
- a user-created linked worktree in a sibling directory (`agent-service-sys-7240`).

So a project memory formed in a worktree (a) never fires from the canonical repo
checkout, and (b) is permanently orphaned when the worktree is removed. The live
DB had ~37 such memories bound to deleted worktree paths.

## Decision

Collapse a **worktree** cwd to its stable main-worktree root before slugifying —
and only a worktree; a normal checkout or any subdirectory of one is left
unchanged, so existing exact-cwd scoping is preserved.

`utils.canonical_project_cwd(cwd, *, use_git=False)`:

1. **Always (pure string, hot-path safe):** a harness segment
   `<repo>/.claude/worktrees/agent-<id>[/...]` collapses to `<repo>`.
2. **`use_git=True` only:** a linked worktree is detected via `git rev-parse
   --git-dir --git-common-dir` (they diverge only inside a linked worktree; the
   main worktree root is the parent of `--git-common-dir`) and collapsed to that
   root. The main worktree and its subdirs are untouched.

Fail-open: any error (not a repo, git missing, worktree already deleted) returns
`cwd` unchanged.

Call sites use `project_slug_for_cwd(cwd, use_git=…)`:

- **Formation** (`_resolve_project_slug`) runs in the background watcher →
  `use_git=True` (affords the git lookup; collapses both worktree kinds).
- **Matching** (`pretool.py` PreToolUse, and `_failure_surface.py` — the shared
  failure-moment match seam behind `post_tool_failure.py` / Codex `post_tool.py`)
  → `use_git=False` (a substring check, no subprocess), so a harness worktree
  still resolves to the same slug formation used without breaking the
  single-digit-ms latency budget.

## Alternatives considered

- **Canonicalize with git on the match hot path too:** would also fire memories
  while inside a *sibling* worktree, but adds a `git` subprocess (~5–15 ms) to
  every tool call — against the stdlib-only, single-digit-ms hot-path rule.
  Rejected; the memory still binds to the stable repo and fires from the main
  checkout (the dominant case).
- **Change `slugify_cwd` itself:** it also derives the on-disk transcript path
  (`derive_transcript_path`), which must match Claude Code's *raw*-cwd layout.
  Collapsing there would break transcript lookup for worktree sessions. Rejected;
  a separate helper keeps the two concerns apart.
- **Collapse every cwd to its repo root (not just worktrees):** would make all
  project memories repo-scoped instead of cwd-scoped — a larger semantic change
  than the bug warrants, and it would break the formation/match symmetry (the
  hot path can't run git to collapse a subdir). Rejected.

## Consequences

- New worktree-formed project memories bind to the repo and survive worktree
  removal; harness-worktree sessions surface project memories correctly on both
  paths.
- Edge: a memory formed inside a *user-created sibling* worktree won't re-surface
  while still inside that same worktree (match is git-free); it fires from the
  main checkout. Acceptable — the prior behavior lost it entirely.
- **Pre-existing** stranded memories are not repaired by this go-forward change
  (a removed sibling worktree's path can't be resolved back to its repo). Left as
  a consolidation/cleanup follow-up.
