"""Formation near-duplicate gate: `engram remember` surfaces top-3 similar
memories and withholds the insert on a strong match, so the (remember-only)
formation agent can merge with --into or insist with --force. See docs/adr/0014.
"""

from __future__ import annotations

import json

from toolengrams import memory_store
from toolengrams.cli import remember
from toolengrams.formation import find_similar, score_pair

# Two near-identical bodies with NON-overlapping triggers — so the trigger-based
# find_overlapping_memory misses them and the semantic gate is what must catch it.
_BODY_A = ("Without this memory the agent would forget that macos has no timeout "
           "command and must use gtimeout from coreutils instead")
_BODY_DUP = _BODY_A + " when running shell commands"
_BODY_DIFFERENT = ("Without this memory the agent would push to a protected git "
                   "branch instead of opening a pull request first")


def _count(conn):
    return conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]


def _remember(args, capsys):
    rc = remember.main(args)
    out = json.loads(capsys.readouterr().out)
    return rc, out


def _seed_a(capsys):
    rc, out = _remember(
        [_BODY_A, "--name", "macos-timeout-a", "--scope", "global",
         "--trigger", "alpha beta"], capsys)
    assert out["action"] == "inserted"
    return out["memory"]["id"]


# ---------- find_similar primitive ----------


def test_find_similar_ranks_and_excludes(temp_db, capsys):
    aid = _seed_a(capsys)
    hits = find_similar(temp_db, "macos-timeout-x", _BODY_DUP, limit=3)
    assert hits and hits[0][0].id == aid
    assert hits[0][1] >= 0.6                       # high token-Jaccard

    excluded = find_similar(temp_db, "macos-timeout-x", _BODY_DUP, exclude_id=aid)
    assert all(m.id != aid for m, _ in excluded)   # exclude_id is honored


def test_score_pair_is_pairwise_jaccard():
    # score_pair is the same token-Jaccard metric find_similar ranks by, computed
    # directly on two (name, body) pairs with no DB/FTS — the seam the collision
    # gate uses when it already holds both texts.
    near = score_pair("macos-timeout-a", _BODY_A, "macos-timeout-b", _BODY_DUP)
    assert near >= 0.6                             # near-duplicate bodies
    far = score_pair("macos-timeout-a", _BODY_A, "git-branch", _BODY_DIFFERENT)
    assert far < near
    assert far < 0.3                               # unrelated facts
    # Empty side → 0.0 (mirrors jaccard's guard, no crash).
    assert score_pair("n", "", "m", "anything at all") == 0.0


# ---------- the gate ----------


def test_near_duplicate_is_withheld_for_review(temp_db, capsys):
    _seed_a(capsys)
    before = _count(temp_db)
    rc, out = _remember(
        [_BODY_DUP, "--name", "macos-timeout-b", "--scope", "global",
         "--trigger", "gamma delta"], capsys)
    assert rc == 0
    assert out["action"] == "review_similar"        # NOT inserted
    assert out["candidates"][0]["name"] == "macos-timeout-a"
    assert "--into" in out["guidance"]
    assert _count(temp_db) == before                 # nothing written


def test_force_inserts_despite_similar(temp_db, capsys):
    _seed_a(capsys)
    before = _count(temp_db)
    rc, out = _remember(
        [_BODY_DUP, "--name", "macos-timeout-b", "--scope", "global",
         "--trigger", "gamma delta", "--force"], capsys)
    assert out["action"] == "inserted"
    assert _count(temp_db) == before + 1
    # Advisory neighbors still surfaced on the forced insert.
    assert out["similar_memories"][0]["name"] == "macos-timeout-a"


def test_into_merges_into_existing(temp_db, capsys):
    aid = _seed_a(capsys)
    before = _count(temp_db)
    rc, out = _remember(
        [_BODY_DUP + " MERGED", "--name", "macos-timeout-a", "--scope", "global",
         "--trigger", "gamma delta", "--into", str(aid)], capsys)
    assert rc == 0
    assert out["action"] == "merged_into"
    assert out["merged_into"] == aid
    assert _count(temp_db) == before                 # no new row
    assert "MERGED" in memory_store.get(temp_db, aid).body


def test_into_triggerless_body_preserves_target_triggers(temp_db, capsys):
    # A merge body with no extractable triggers and no --trigger must NOT wipe
    # the target's triggers (it folds into an already-triggered memory).
    aid = _seed_a(capsys)
    before = temp_db.execute(
        "SELECT COUNT(*) c FROM triggers WHERE memory_id=?", (aid,)).fetchone()["c"]
    assert before >= 1

    rc, out = _remember(
        [_BODY_DUP + " extra detail", "--name", "macos-timeout-a",
         "--scope", "global", "--into", str(aid)], capsys)  # no --trigger
    assert rc == 0 and out["action"] == "merged_into"
    after = temp_db.execute(
        "SELECT COUNT(*) c FROM triggers WHERE memory_id=?", (aid,)).fetchone()["c"]
    assert after == before                          # triggers preserved, not wiped


def test_into_without_name_keeps_target_name(temp_db, capsys):
    # A merge that omits --name must NOT clobber the target's name with the
    # synthesized "Without this memory…" body-as-name (the live regression).
    aid = _seed_a(capsys)  # name: macos-timeout-a
    rc, out = _remember(
        [_BODY_DUP + " more", "--scope", "global", "--into", str(aid)], capsys)  # no --name
    assert rc == 0 and out["action"] == "merged_into"
    assert memory_store.get(temp_db, aid).name == "macos-timeout-a"


def test_overlap_resave_withholds_and_preserves_existing(temp_db, capsys):
    # The trigger-overlap path no longer auto-merges (that silently overwrote
    # memories): a re-save sharing a trigger is WITHHELD for review, and the
    # existing memory's name and body are left untouched.
    aid = _seed_a(capsys)  # name: macos-timeout-a, trigger: "alpha beta"
    before = memory_store.get(temp_db, aid)
    rc, out = _remember(
        [_BODY_A + " updated", "--scope", "global", "--trigger", "alpha beta"], capsys)
    assert rc == 0
    assert out["action"] == "review_collision"
    assert out["collision"]["id"] == aid
    after = memory_store.get(temp_db, aid)
    assert after.name == "macos-timeout-a"     # name untouched
    assert after.body == before.body           # body untouched — no overwrite

    # --force still lets an insistent caller create a distinct memory sharing it.
    rc, out = _remember(
        [_BODY_A + " forced", "--name", "macos-timeout-c", "--scope", "global",
         "--trigger", "alpha beta", "--force"], capsys)
    assert out["action"] == "inserted"


def test_into_nonexistent_errors(temp_db, capsys):
    rc, out = _remember(
        [_BODY_A, "--name", "x", "--scope", "global",
         "--trigger", "a b", "--into", "9999"], capsys)
    assert rc == 1
    assert out["error"] == "into_not_found"


def test_dissimilar_inserts_normally(temp_db, capsys):
    _seed_a(capsys)
    before = _count(temp_db)
    rc, out = _remember(
        [_BODY_DIFFERENT, "--name", "git-pr-first", "--scope", "global",
         "--trigger", "git push"], capsys)
    assert out["action"] == "inserted"
    assert _count(temp_db) == before + 1
