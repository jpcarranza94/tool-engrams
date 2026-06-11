"""engram edit — in-place correction preserving identity and history (ADR-0007)."""

from __future__ import annotations

import json
import time

from toolengrams import memory_store
from toolengrams.cli import edit


def _seed(conn, *, name="gh merge lore", body="Use `gh pr merge` after checks.",
          useful=3, noise=1, surfaces=7) -> int:
    mid = memory_store.insert_memory(
        conn, name=name, description="d", body=body, kind="hint",
        scope="global", project_slug=None, pinned=False,
        created_ts=int(time.time()),
    )
    memory_store.add_token_trigger(conn, mid, ["gh", "pr", "merge"])
    conn.execute(
        "UPDATE memories SET useful_count = ?, noise_count = ?, surface_count = ? "
        "WHERE id = ?", (useful, noise, surfaces, mid),
    )
    return mid


def test_edit_body_preserves_counters_and_triggers(temp_db, capsys):
    mid = _seed(temp_db)
    assert edit.main([str(mid), "--body",
                      "Use `gh pr merge` only after mergeStateStatus is CLEAN."]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "edited"
    assert out["preserved"] == {"surface_count": 7, "useful_count": 3, "noise_count": 1}

    mem = memory_store.get(temp_db, mid)
    assert "CLEAN" in mem.body
    assert mem.useful_count == 3 and mem.surface_count == 7
    assert mem.last_verified_ts is not None  # correction = freshness signal
    assert len(memory_store.triggers_for(temp_db, mid)) == 1  # untouched


def test_edit_resolves_by_name(temp_db, capsys):
    _seed(temp_db, name="gh merge lore")
    assert edit.main(["gh merge lore", "--body", "new `gh pr merge` body"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "edited"


def test_edit_re_extract_triggers(temp_db, capsys):
    mid = _seed(temp_db)
    assert edit.main([str(mid), "--body",
                      "Always run `docker compose up` before `pytest`.",
                      "--re-extract-triggers"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["triggers_re_extracted"] >= 1
    firsts = {t.first_token for t in memory_store.triggers_for(temp_db, mid)
              if t.kind == "token_subseq"}
    assert "gh" not in firsts  # old trigger gone


def test_edit_rejects_secrets(temp_db, capsys):
    mid = _seed(temp_db)
    rc = edit.main([str(mid), "--body",
                    "use `curl` with api_key=sk-1234567890abcdef1234567890abcdef"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "contains_secrets"
    assert "api_key" not in memory_store.get(temp_db, mid).body  # unchanged


def test_edit_requires_a_change(temp_db, capsys):
    mid = _seed(temp_db)
    assert edit.main([str(mid)]) == 2


def test_edit_not_found(temp_db, capsys):
    assert edit.main(["999", "--body", "x `y z`"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "not_found"


def test_edit_refuses_re_extract_that_orphans(temp_db, capsys):
    """A re-extract finding zero triggers must refuse, not orphan the memory."""
    mid = _seed(temp_db)
    rc = edit.main([str(mid), "--body", "prose with no commands or paths at all",
                    "--re-extract-triggers"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "no_triggers"
    assert len(memory_store.triggers_for(temp_db, mid)) == 1  # untouched
