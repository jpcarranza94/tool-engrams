"""`engram resolve-slug` + utils.unslugify_candidates."""

from __future__ import annotations

import json

from toolengrams.cli import resolve_slug
from toolengrams.utils import slugify_cwd, unslugify_candidates


def test_round_trip_for_simple_path(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    slug = slugify_cwd(str(repo))
    candidates = unslugify_candidates(slug)
    assert repo in candidates


def test_dash_ambiguity_picks_real_dir(tmp_path):
    # /tmpish/parent/tool-engrams (dash in dir name) — the naive split would
    # also propose /tmpish/parent/tool/engrams which won't exist.
    parent = tmp_path / "parent"
    parent.mkdir()
    real = parent / "tool-engrams"
    real.mkdir()
    slug = slugify_cwd(str(real))

    candidates = unslugify_candidates(slug)
    assert real in candidates
    # Phantom split is silently filtered because the dir doesn't exist.
    phantom = tmp_path / "parent" / "tool" / "engrams"
    assert phantom not in candidates


def test_returns_empty_when_no_path_exists():
    slug = "-Users-nope-projects-does-not-exist"
    assert unslugify_candidates(slug) == []


def test_returns_empty_for_invalid_slug():
    assert unslugify_candidates("") == []
    assert unslugify_candidates("not-starting-with-dash") == []


def test_resolve_slug_cli_prints_best_and_candidates(tmp_path, capsys):
    repo = tmp_path / "agentsvc"
    repo.mkdir()
    slug = slugify_cwd(str(repo))

    rc = resolve_slug.main([slug])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["slug"] == slug
    assert payload["best"] == str(repo)
    assert str(repo) in payload["candidates"]


def test_resolve_slug_cli_reports_missing(capsys):
    rc = resolve_slug.main(["-completely-fake-path-12345"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates"] == []
    assert payload["best"] is None
