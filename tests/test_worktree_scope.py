"""Worktree-aware project-scope canonicalization (utils.canonical_project_cwd).

A project-scoped memory binds to slugify_cwd(cwd) under an exact-cwd filter. A
git worktree cwd is ephemeral — the memory never fires from the canonical repo
and dies when the worktree is removed. These tests pin the collapse-to-main-repo
behavior, and that NORMAL checkouts/subdirs are left untouched.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from toolengrams.cli.remember import _resolve_project_slug
from toolengrams.utils import (
    canonical_project_cwd,
    project_slug_for_cwd,
    slugify_cwd,
)

REPO = "/Users/dev/projects/tool-engrams"


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


# ---------- harness worktree: pure string, works on the hot path too ----------

@pytest.mark.parametrize("use_git", [False, True])
def test_harness_worktree_collapses_to_repo_root(use_git):
    wt = f"{REPO}/.claude/worktrees/agent-abc123"
    assert canonical_project_cwd(wt, use_git=use_git) == REPO


@pytest.mark.parametrize("use_git", [False, True])
def test_harness_worktree_subdir_collapses(use_git):
    wt = f"{REPO}/.claude/worktrees/agent-abc123/toolengrams/cli"
    assert canonical_project_cwd(wt, use_git=use_git) == REPO


def test_non_worktree_cwd_unchanged_cheap():
    # A normal subdir must NOT be collapsed on the hot path (exact-cwd scoping).
    sub = f"{REPO}/frontend"
    assert canonical_project_cwd(sub, use_git=False) == sub


def test_project_slug_for_cwd_collapses_harness_worktree():
    wt = f"{REPO}/.claude/worktrees/agent-x"
    assert project_slug_for_cwd(wt) == slugify_cwd(REPO)


def test_empty_cwd_is_noop():
    assert canonical_project_cwd("", use_git=True) == ""


# ---------- real git: linked worktree collapses, main worktree preserved ----------

@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_linked_worktree_collapses_to_main(git_repo, tmp_path):
    wt = tmp_path / "myrepo-feature"
    _git(git_repo, "worktree", "add", "-q", str(wt))
    got = canonical_project_cwd(str(wt), use_git=True)
    assert Path(got).resolve() == git_repo.resolve()


def test_main_worktree_unchanged_with_git(git_repo):
    # The main checkout is NOT collapsed — existing exact-cwd scoping preserved.
    assert canonical_project_cwd(str(git_repo), use_git=True) == str(git_repo)


def test_main_worktree_subdir_unchanged_with_git(git_repo):
    sub = git_repo / "pkg"
    sub.mkdir()
    assert canonical_project_cwd(str(sub), use_git=True) == str(sub)


def test_linked_worktree_ignored_without_git(git_repo, tmp_path):
    # use_git=False (hot path) leaves a non-harness linked worktree alone.
    wt = tmp_path / "myrepo-feature2"
    _git(git_repo, "worktree", "add", "-q", str(wt))
    assert canonical_project_cwd(str(wt), use_git=False) == str(wt)


def test_non_git_dir_fail_open(tmp_path):
    plain = tmp_path / "notarepo"
    plain.mkdir()
    assert canonical_project_cwd(str(plain), use_git=True) == str(plain)


def test_missing_path_fail_open():
    p = "/nonexistent/path/xyz-does-not-exist"
    assert canonical_project_cwd(p, use_git=True) == p


# ---------- formation wiring (_resolve_project_slug) ----------

def test_resolve_project_slug_collapses_harness_worktree():
    wt = f"{REPO}/.claude/worktrees/agent-x"
    assert _resolve_project_slug("project", None, wt) == slugify_cwd(REPO)


def test_resolve_project_slug_global_is_none():
    wt = f"{REPO}/.claude/worktrees/agent-x"
    assert _resolve_project_slug("global", None, wt) is None


def test_resolve_project_slug_explicit_override_wins():
    wt = f"{REPO}/.claude/worktrees/agent-x"
    assert _resolve_project_slug("project", "explicit-slug", wt) == "explicit-slug"
