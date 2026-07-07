"""Shared utility functions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Env var set on each detached watcher-tick by spawn_tick. Any `claude` the
# tick launches inherits it, so the SessionStart / UserPromptSubmit hooks
# running inside that child can refuse to spawn yet another watcher. This is
# the recursion guard hooks check first: if `--bare` ever stops suppressing
# hooks (the May-2026 recursive-spawn burst), this still stops the recursion.
WATCHER_CHILD_ENV = "ENGRAM_IN_WATCHER"

# Env var set on the nightly consolidation agent's engine child (agent.py). The
# agent is the one automated context trusted with the full `engram` verb set, so
# unlike the watcher it is NOT verb-restricted — this marker lets a maintainer-
# only verb (`recommend --close`) refuse to run under the agent even though the
# sandbox would allow it.
CONSOLIDATION_CHILD_ENV = "ENGRAM_IN_CONSOLIDATION"


def is_watcher_child() -> bool:
    """True if this process was spawned by (or inside) the watcher subprocess."""
    return os.environ.get(WATCHER_CHILD_ENV) == "1"


def is_consolidation_child() -> bool:
    """True if this process is running inside the nightly consolidation agent."""
    return os.environ.get(CONSOLIDATION_CHILD_ENV) == "1"


def env_int(name: str, default: int) -> int:
    """Read an int tuning knob from os.environ, falling back to `default` when
    unset or unparseable. Read at CALL time (not import) so config.hydrate_env()
    — which runs in __main__ after all modules import — has populated the value.
    """
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    """Float counterpart of env_int (e.g. the q gate / similarity thresholds)."""
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        return default


def prepend_engram_bin(env: dict[str, str]) -> dict[str, str]:
    """Prepend this interpreter's bin dir to env['PATH'] (mutates and returns).

    The watcher and consolidation agents grant their `claude -p` child an
    allowlist of `engram` verbs — the child shell must resolve `engram` by
    name. Under install.sh's venv fallback (PEP 668 machines), engram lives
    in a private venv the global PATH may not have; the venv's bin dir (next
    to sys.executable, where console scripts land) does.
    """
    bin_dir = str(Path(sys.executable).parent)
    path = env.get("PATH", "")
    if bin_dir not in path.split(os.pathsep):
        env["PATH"] = f"{bin_dir}{os.pathsep}{path}" if path else bin_dir
    return env


def slugify_cwd(cwd: str) -> str:
    """Match Claude Code's project-slug convention: `/` → `-`."""
    return cwd.replace("/", "-")


# A Claude Code harness worktree lives at `<repo>/.claude/worktrees/agent-<id>`
# (Agent isolation:'worktree'). Everything from this marker on is ephemeral, so a
# project memory bound to it dies when the worktree is removed. Pure-string, so
# it is safe to collapse on the PreToolUse hot path.
_HARNESS_WORKTREE_MARKER = "/.claude/worktrees/"


def canonical_project_cwd(cwd: str, *, use_git: bool = False) -> str:
    """Collapse a git *worktree* cwd to its stable main-repo root.

    Project-scoped memories bind to ``slugify_cwd(cwd)`` under an EXACT-match cwd
    filter (see rank.py). A worktree cwd is ephemeral: the memory never fires from
    the canonical repo and vanishes when the worktree is removed. Collapsing a
    worktree cwd to its main worktree root fixes both.

    A NORMAL checkout — or any subdirectory of one — is returned UNCHANGED, so the
    existing exact-cwd project scoping is preserved; only worktree paths move.

    - Always (pure string, hot-path safe): a Claude Code harness worktree segment
      ``<repo>/.claude/worktrees/agent-<id>[/...]`` collapses to ``<repo>``.
    - ``use_git=True`` (formation only — NEVER the PreToolUse hot path): a
      user-created *linked* worktree (a sibling directory) collapses to its main
      worktree root, detected via ``git rev-parse``. The main worktree and its
      subdirs are left untouched.

    Fail-open: returns ``cwd`` unchanged on anything unexpected (not a repo, git
    missing/erroring, worktree already deleted).
    """
    marker = cwd.find(_HARNESS_WORKTREE_MARKER)
    if marker != -1:
        return cwd[:marker]
    if not use_git:
        return cwd
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute",
             "--git-dir", "--git-common-dir"],
            capture_output=True, text=True, timeout=3, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd  # not a repo / git missing / non-zero exit → leave cwd as-is
    lines = out.stdout.splitlines()
    if len(lines) < 2:
        return cwd
    git_dir, common_dir = lines[0].strip(), lines[1].strip()
    # In the main worktree (or a subdir of it) --git-dir == --git-common-dir. They
    # diverge ONLY inside a linked worktree, where --git-common-dir points at the
    # main repo's `.git` — its parent is the main worktree root.
    if common_dir and git_dir != common_dir and common_dir.endswith("/.git"):
        return os.path.dirname(common_dir)
    return cwd


def project_slug_for_cwd(cwd: str, *, use_git: bool = False) -> str:
    """``slugify_cwd`` of the canonical repo root of ``cwd`` (worktree-aware).

    Use ``use_git=True`` off the hot path (formation), ``False`` on it (matching).
    """
    return slugify_cwd(canonical_project_cwd(cwd, use_git=use_git))


def safe_filename_id(name: str) -> str:
    """Sanitize an externally-supplied id (e.g. a hook's session_id) for use as
    a filename component: alnum / `-` / `_` pass through, anything else becomes
    `_`, capped at 120 chars. Real session ids are UUIDs and pass unchanged;
    this exists so a hostile or malformed id can't traverse out of the dir it
    names (lock files, sandbox cwds)."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:120]


def unslugify_candidates(slug: str) -> list[Path]:
    """Enumerate candidate paths that could have produced this Claude Code slug.

    `slugify_cwd` is lossy: directory names containing `-` (like `tool-engrams`)
    are indistinguishable from path separators after slugification. This walks
    the slug and yields every plausible `/`-split, then keeps only those that
    exist on disk. Returns paths in length order (deepest first) so callers
    that just want "the best guess" can take [0].

    Example:
        unslugify_candidates("-Users-jpcar-personal-projects-tool-engrams")
        -> [PosixPath('/Users/dev/projects/tool-engrams'), ...]
    """
    if not slug or not slug.startswith("-"):
        return []
    # Strip leading `-` (the original `/`), then split on each remaining `-`.
    tokens = slug[1:].split("-")
    candidates: list[Path] = []
    # Try every contiguous grouping: each `-` is either a `/` or a literal `-`.
    # For N tokens there are 2^(N-1) groupings — but realistic slugs have
    # <12 tokens so 4096 paths max, each a quick exists() check.
    n = len(tokens)
    if n == 0:
        return []
    for mask in range(1 << (n - 1)):
        parts: list[str] = [tokens[0]]
        for i in range(n - 1):
            if mask & (1 << i):
                parts[-1] = parts[-1] + "-" + tokens[i + 1]
            else:
                parts.append(tokens[i + 1])
        path = Path("/" + "/".join(parts))
        if path.is_dir():
            candidates.append(path)
    # Deepest match first so caller-prefers-longest behavior is natural.
    candidates.sort(key=lambda p: -len(p.parts))
    return candidates
