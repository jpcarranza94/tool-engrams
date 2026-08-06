"""A relative path_glob can never fnmatch an absolute call path (retrieval only
ever emits absolute paths, and fnmatch has no implicit anchor), so it is rooted
at `**/` on write — and v19 backfills the rows written before that."""

from __future__ import annotations

import fnmatch
import sqlite3
import time
from pathlib import Path

from toolengrams import db, memory_store
from toolengrams.cli import trigger


def test_relative_glob_is_rooted_on_write_and_then_matches(temp_db, capsys):
    mid = memory_store.insert_memory(
        temp_db, name="n", description="", body="b", kind="hint", scope="global",
        project_slug=None, pinned=False, created_ts=int(time.time()),
    )
    memory_store.add_path_trigger(temp_db, mid, "**/keep.py")  # so it can't orphan
    assert trigger.main([str(mid), "--add-path", "app/db/**"]) == 0
    capsys.readouterr()

    pat = [t.path_pattern for t in memory_store.triggers_for(temp_db, mid)
           if t.path_pattern != "**/keep.py"][0]
    assert pat == "**/app/db/**"
    call_path = "/Users/x/repo/app/db/models.py"
    assert not fnmatch.fnmatchcase(call_path, "app/db/**")  # the defect
    assert fnmatch.fnmatchcase(call_path, pat)              # the fix


def test_v18_db_roots_relative_path_globs(tmp_path: Path):
    path = tmp_path / "v18.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(db.SCHEMA_PATH.read_text())
    raw.execute(
        "INSERT INTO memories (id, name, description, body, kind, scope, "
        " project_slug, created_ts, useful_count, noise_count, surface_count) "
        "VALUES (1, 'm', '', 'b', 'hint', 'global', NULL, 1, 7, 2, 9)")
    for pat in ("app/db/**", "**/app/db/**", "**/already.py", "/etc/hosts",
                "README.md"):
        raw.execute("INSERT INTO triggers (memory_id, kind, path_pattern) "
                    "VALUES (1, 'path_glob', ?)", (pat,))
    raw.execute("PRAGMA user_version = 18")
    raw.commit()
    raw.close()

    conn = db.connect(path)
    pats = sorted(r["path_pattern"] for r in
                  conn.execute("SELECT path_pattern FROM triggers").fetchall())
    # 'app/db/**' collapsed into the existing '**/app/db/**' (no duplicate);
    # rooted/absolute rows untouched; a bare basename is deliberately skipped
    # ('**/README.md' is exactly what the specificity gate refuses).
    assert pats == ["**/already.py", "**/app/db/**", "/etc/hosts", "README.md"]
    row = conn.execute("SELECT useful_count, noise_count FROM memories "
                       "WHERE id = 1").fetchone()
    assert (row["useful_count"], row["noise_count"]) == (7, 2)  # counter-preserving
    conn.close()
