"""Tests for consolidation.collect — particularly the internal-project skip filter.

The skip filter regressed once already (substring + endswith logic was both
too loose and wrong-shape). These tests pin down the contract: only slugs
ending with our temp-dir naming pattern are skipped, and project names that
merely contain those words mid-path are NOT skipped.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import pytest

from toolengrams.consolidation.collect import (
    _INTERNAL_PROJECT_RE,
    _is_internal_project,
    collect_sessions,
)


# ---------- _is_internal_project ----------


@pytest.mark.parametrize("slug", [
    "-var-folders-2x-abc123-T-engram-consolidate-Z3K9q1",
    "-tmp-engram-observe-abc123",
    "-private-var-folders-T-engram-experiment-XYZ",
    # Watcher stable sandboxes are named after the work session UUID.
    "-private-var-folders-T-engram-formation-0b00831d-526f-438f-aaf8-5616e63e399a",
    "-tmp-engram-eval-0b00831d-526f-438f-aaf8-5616e63e399a",
    # Legacy watcher mkdtemp dirs (pre-stable-sandbox) — random dash-free suffix.
    "-tmp-engram-formation-Xy3kQz",
])
def test_is_internal_project_true_for_our_temp_dirs(slug: str) -> None:
    assert _is_internal_project(slug) is True


@pytest.mark.parametrize("slug", [
    "-Users-jpcar-personal-projects-tool-engrams",
    # User projects that mention our names mid-path must NOT be skipped.
    "-Users-jpcar-engram-consolidate-fork-src",
    # No suffix → not a temp dir (the trailing dash is the problem case the
    # original endswith() logic was supposed to catch but never could).
    "-tmp-engram-consolidate-",
    # Substring-not-suffix case the old `prefix in slug` logic broke on.
    "-Users-jpcar-engram-experiment-research-2024-data",
    # Dashed suffix that is not a UUID must not match the watcher arm.
    "-Users-jpcar-engram-eval-research-2024-data",
])
def test_is_internal_project_false_for_user_projects(slug: str) -> None:
    assert _is_internal_project(slug) is False


def test_internal_regex_anchors_at_end() -> None:
    # Sanity: the regex must require end-of-string after the random suffix.
    # If we relax that, project paths can re-introduce the original bug.
    assert _INTERNAL_PROJECT_RE.pattern.endswith("$")


# ---------- collect_sessions ----------


def test_collect_sessions_skips_internal_projects(tmp_path: Path) -> None:
    """A real-shape projects dir: one user project, one internal temp dir."""
    user_proj = tmp_path / "-Users-jpcar-real-project"
    internal = tmp_path / "-var-folders-T-engram-consolidate-ABC123"
    user_proj.mkdir()
    internal.mkdir()

    today = date.today()
    today_ts = datetime.combine(today, datetime.min.time()).timestamp() + 3600

    user_session = user_proj / "session-user.jsonl"
    user_session.write_text("{}\n")
    os.utime(user_session, (today_ts, today_ts))

    internal_session = internal / "session-internal.jsonl"
    internal_session.write_text("{}\n")
    os.utime(internal_session, (today_ts, today_ts))


    results = collect_sessions(today, projects_dir=tmp_path)

    assert len(results) == 1
    assert results[0].session_id == "session-user"


def test_collect_sessions_does_not_drop_user_projects_with_engram_in_name(
    tmp_path: Path,
) -> None:
    """Regression: the old substring filter dropped legitimate user projects
    whose name happened to contain `engram-consolidate` etc."""
    sneaky = tmp_path / "-Users-jpcar-engram-consolidate-fork-src"
    sneaky.mkdir()

    today = date.today()
    today_ts = datetime.combine(today, datetime.min.time()).timestamp() + 3600

    session = sneaky / "session-real.jsonl"
    session.write_text("{}\n")
    os.utime(session, (today_ts, today_ts))

    results = collect_sessions(today, projects_dir=tmp_path)
    assert len(results) == 1
    assert results[0].session_id == "session-real"
