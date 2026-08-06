"""Unit tests for consolidation CLI and session collection."""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from toolengrams import memory_store
from toolengrams.cli import consolidate
from toolengrams.consolidation import agent, runs
from toolengrams.engine import EngineResult
from toolengrams.retrieval import session_state
from toolengrams.target.claude_code.collect import collect_sessions
from toolengrams.target.interface import SessionFile


# ---------- session collection ----------


def test_collect_finds_jsonl_from_target_date(tmp_path):
    project_dir = tmp_path / "projects" / "my-project"
    project_dir.mkdir(parents=True)
    jsonl = project_dir / "abc-123.jsonl"
    jsonl.write_text('{"type":"user"}\n')

    sessions = collect_sessions(date.today(), projects_dir=tmp_path / "projects")
    assert len(sessions) == 1
    assert sessions[0].session_id == "abc-123"
    assert sessions[0].project_slug == "my-project"


def test_collect_ignores_other_dates(tmp_path):
    project_dir = tmp_path / "projects" / "my-project"
    project_dir.mkdir(parents=True)
    jsonl = project_dir / "old-session.jsonl"
    jsonl.write_text('{"type":"user"}\n')
    # Backdate the file.
    old_ts = time.time() - 86400 * 5
    os.utime(jsonl, (old_ts, old_ts))

    sessions = collect_sessions(date.today(), projects_dir=tmp_path / "projects")
    assert len(sessions) == 0


def test_consolidation_collects_wired_targets_in_timestamp_order(monkeypatch):
    target = date.today()
    claude_session = SessionFile(
        path=Path("/sessions/claude.jsonl"),
        session_id="claude-session",
        project_slug="proj",
        modified_ts=20,
        size_bytes=200,
    )
    codex_session = SessionFile(
        path=Path("/sessions/codex.jsonl"),
        session_id="codex-session",
        project_slug="proj",
        modified_ts=10,
        size_bytes=100,
    )
    skipped = SimpleNamespace(
        NAME="off",
        is_wired=lambda: False,
        collect_sessions=lambda target_date: (_ for _ in ()).throw(
            AssertionError("unwired target called")
        ),
    )
    targets = {
        "claude-code": SimpleNamespace(
            NAME="claude-code",
            is_wired=lambda: True,
            collect_sessions=lambda target_date: [claude_session],
        ),
        "codex": SimpleNamespace(
            NAME="codex",
            is_wired=lambda: True,
            collect_sessions=lambda target_date: [codex_session],
        ),
        "off": skipped,
    }
    monkeypatch.setattr(consolidate, "TARGETS", targets)

    sessions = consolidate.collect_sessions(target)

    assert [(s.target, s.session_id) for s in sessions] == [
        ("codex", "codex-session"),
        ("claude-code", "claude-session"),
    ]


def test_consolidation_collects_other_targets_when_one_target_fails(
    monkeypatch, capsys,
):
    target = date.today()
    good_session = SessionFile(
        path=Path("/sessions/claude.jsonl"),
        session_id="claude-session",
        project_slug="proj",
        modified_ts=20,
        size_bytes=200,
    )
    targets = {
        "claude-code": SimpleNamespace(
            NAME="claude-code",
            is_wired=lambda: True,
            collect_sessions=lambda target_date: [good_session],
        ),
        "codex": SimpleNamespace(
            NAME="codex",
            is_wired=lambda: True,
            collect_sessions=lambda target_date: (_ for _ in ()).throw(
                RuntimeError("bad rollout")
            ),
        ),
    }
    monkeypatch.setattr(consolidate, "TARGETS", targets)

    sessions = consolidate.collect_sessions(target)

    assert [(s.target, s.session_id) for s in sessions] == [
        ("claude-code", "claude-session"),
    ]
    assert "codex collection failed: bad rollout" in capsys.readouterr().err


def test_consolidation_prompt_session_list_includes_target(monkeypatch, tmp_path):
    captured = {}

    def invoke(req):
        captured["prompt"] = req.prompt
        return EngineResult(ok=True, returncode=0, text="done")

    fake_engine = SimpleNamespace(
        NAME="fake",
        is_available=lambda: True,
        prepare_sandbox=lambda path, spec: None,
        invoke=invoke,
    )
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setattr(agent, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(agent, "_get_memory_summary", lambda path: "memory summary")
    session = SessionFile(
        path=tmp_path / "rollout.jsonl",
        session_id="session-abcdef",
        project_slug="proj",
        modified_ts=1,
        size_bytes=1024,
        target="codex",
    )

    result = agent.run_consolidation_agent([session], db_path, "2026-06-12")

    assert result.returncode == 0
    assert "[codex]" in captured["prompt"]
    assert str(session.path) in captured["prompt"]


# ---------- CLI ----------


def test_consolidate_dry_run(temp_db, monkeypatch, capsys):
    rc = consolidate.main(["--dry-run", "--json"])
    assert rc == 0


def test_consolidate_idempotent(temp_db, monkeypatch, capsys):
    # Simulate a previous run by inserting directly.
    today = date.today().isoformat()
    temp_db.execute(
        "INSERT INTO consolidation_runs (run_date, started_ts, completed_ts, sessions_scanned, report) "
        "VALUES (?, ?, ?, 0, 'done')",
        (today, int(time.time()), int(time.time())),
    )
    rc = consolidate.main(["--json"])
    out = capsys.readouterr().out.strip()
    result = json.loads(out)
    assert result["status"] == "completed"
    assert result["runs"] == [{"status": "already_run", "run_date": today}]


# ---------- catch-up backfill ----------


def _one_session():
    return SessionFile(
        path=Path("/sessions/s.jsonl"),
        session_id="s",
        project_slug="proj",
        modified_ts=1,
        size_bytes=10,
    )


def _ok_agent(report='{"metrics": {"surfaces_evaluated": 2}}'):
    return SimpleNamespace(error=None, report=report, returncode=0)


def test_resolve_dates_yesterday_is_catchup_window():
    dates = consolidate._resolve_dates(SimpleNamespace(date=None, yesterday=True))
    today = date.today()
    expected = [today - timedelta(days=n)
                for n in range(consolidate.CATCHUP_LOOKBACK_DAYS, 0, -1)]
    assert dates == expected
    assert dates[-1] == today - timedelta(days=1)   # ends on yesterday
    assert dates[0] < dates[-1]                      # oldest first


def test_catchup_backfills_only_days_with_sessions(temp_db, monkeypatch, capsys):
    today = date.today()
    gap = (today - timedelta(days=3)).isoformat()

    # Sessions exist only on the 3-days-ago gap day.
    def fake_collect(target_date):
        return [_one_session()] if target_date.isoformat() == gap else []
    monkeypatch.setattr(consolidate, "collect_sessions", fake_collect)

    ran = []

    def fake_agent(*, sessions, db_path, target_date):
        ran.append(target_date)
        return _ok_agent()
    monkeypatch.setattr(consolidate, "run_consolidation_agent", fake_agent)

    rc = consolidate.main(["--yesterday", "--json"])
    assert rc == 0
    assert ran == [gap]                                  # only the day with sessions
    assert consolidate.runs.was_run(temp_db, gap)        # and it was recorded


def test_catchup_skips_already_run_days(temp_db, monkeypatch):
    today = date.today()
    done = (today - timedelta(days=2)).isoformat()
    temp_db.execute(
        "INSERT INTO consolidation_runs (run_date, started_ts, completed_ts, sessions_scanned, report) "
        "VALUES (?, ?, ?, 1, 'done')",
        (done, int(time.time()), int(time.time())),
    )

    monkeypatch.setattr(consolidate, "collect_sessions", lambda d: [_one_session()])

    ran = []

    def fake_agent(*, sessions, db_path, target_date):
        ran.append(target_date)
        return _ok_agent()
    monkeypatch.setattr(consolidate, "run_consolidation_agent", fake_agent)

    consolidate.main(["--yesterday", "--json"])
    assert done not in ran                                # recorded day never re-run


def test_catchup_error_day_is_not_recorded_so_it_retries(temp_db, monkeypatch):
    today = date.today()
    target = (today - timedelta(days=1)).isoformat()

    def fake_collect(target_date):
        return [_one_session()] if target_date.isoformat() == target else []
    monkeypatch.setattr(consolidate, "collect_sessions", fake_collect)

    def fake_agent(*, sessions, db_path, target_date):
        return SimpleNamespace(error="spawn failed", report=None, returncode=1)
    monkeypatch.setattr(consolidate, "run_consolidation_agent", fake_agent)

    rc = consolidate.main(["--yesterday", "--json"])
    assert rc == 1                                        # surfaced as failure
    assert not consolidate.runs.was_run(temp_db, target)  # left un-run → retried next time


def test_date_flag_emits_aggregate_shape(temp_db, monkeypatch, capsys):
    # --date (manual backfill) goes through the same aggregate output as the
    # catch-up sweep — pin the {status, surfaces_cleaned, runs:[...]} shape.
    monkeypatch.setattr(consolidate, "collect_sessions", lambda d: [_one_session()])
    monkeypatch.setattr(consolidate, "run_consolidation_agent",
                        lambda **kw: _ok_agent())

    rc = consolidate.main(["--date", "2026-01-02", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "completed"
    assert "surfaces_cleaned" in out
    assert out["runs"] == [{"status": "completed", "run_date": "2026-01-02",
                            "sessions_scanned": 1, "error": None}]


def test_catchup_skips_when_another_sweep_holds_lock(temp_db, monkeypatch, capsys):
    # A second concurrent sweep must exit cleanly without spawning an agent.
    monkeypatch.setattr(
        consolidate, "collect_sessions",
        lambda d: (_ for _ in ()).throw(AssertionError("ran while lock held")),
    )

    lock_dir = consolidate.db.db_path().parent / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    held = open(lock_dir / "consolidate.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = consolidate.main(["--yesterday", "--json"])
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()

    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"action": "skipped", "reason": "already_running"}


# ---------- cold (never-surfaced) memory selection ----------

DAY = 86400


def _insert_mem(conn, name, *, created_ago_days, surface_count=0,
                last_surfaced_ago_days=None):
    mid = memory_store.insert_memory(
        conn, name=name, description=None, body=f"body of {name}",
        kind="hint", scope="global", project_slug=None, pinned=False,
        created_ts=int(time.time()) - created_ago_days * DAY,
    )
    if surface_count:
        # A surfaced memory must carry a real last_surfaced_ts — leaving it 0
        # would make every fixture read as "no surface since the horizon".
        last = int(time.time()) - (last_surfaced_ago_days or 0) * DAY
        conn.execute("UPDATE memories SET surface_count=?, last_surfaced_ts=? "
                     "WHERE id=?", (surface_count, last, mid))
    conn.commit()
    return mid


def _cold_ids(conn, *, days):
    """Run the predicate over the loaded inventory, like _get_memory_summary."""
    memories = memory_store.list_memories(conn, order="audit")
    cold = agent._cold_memories(memories, int(time.time()) - days * DAY)
    return [m.id for m in cold]


def test_cold_memories_selects_old_unsurfaced_only(temp_db):
    cold = _insert_mem(temp_db, "cold-old-unsurfaced", created_ago_days=40)
    _insert_mem(temp_db, "fresh-unsurfaced", created_ago_days=2)
    _insert_mem(temp_db, "old-but-surfaced", created_ago_days=40, surface_count=3)
    # fresh (too new) and surfaced (has fired) are both excluded
    assert _cold_ids(temp_db, days=30) == [cold]


def test_cold_memories_orders_oldest_first(temp_db):
    older = _insert_mem(temp_db, "older", created_ago_days=90)
    newer = _insert_mem(temp_db, "newer", created_ago_days=40)
    assert _cold_ids(temp_db, days=30) == [older, newer]


def test_memory_summary_renders_cold_section(temp_db):
    cold = _insert_mem(temp_db, "cold-old-unsurfaced", created_ago_days=40)
    summary = agent._get_memory_summary(Path(os.environ["ENGRAM_DB"]))
    body = summary.split("Cold — no surface in 30+ days", 1)
    assert len(body) == 2, "cold section header missing"
    assert f'[{cold}] "cold-old-unsurfaced"' in body[1]


def test_memory_summary_no_cold_section_when_none(temp_db):
    _insert_mem(temp_db, "fresh-unsurfaced", created_ago_days=1)
    summary = agent._get_memory_summary(Path(os.environ["ENGRAM_DB"]))
    assert "Cold — no surface" not in summary


def test_cold_horizon_respects_env_override(temp_db, monkeypatch):
    mid = _insert_mem(temp_db, "ten-day-old", created_ago_days=10)
    # default horizon (30d) leaves a 10-day-old memory out; tightening pulls it in
    monkeypatch.setenv("ENGRAM_COLD_MEMORY_DAYS", "7")
    summary = agent._get_memory_summary(Path(os.environ["ENGRAM_DB"]))
    assert "Cold — no surface in 7+ days" in summary
    assert f"[{mid}]" in summary.split("Cold — no surface", 1)[1]


def test_cold_horizon_clamps_nonpositive_env(temp_db, monkeypatch):
    # A 0/negative horizon would push the cutoff to now-or-future and flag a
    # fresh, just-created memory as cold. The clamp to >=1 must prevent that.
    _insert_mem(temp_db, "fresh-unsurfaced", created_ago_days=0)
    monkeypatch.setenv("ENGRAM_COLD_MEMORY_DAYS", "-5")
    summary = agent._get_memory_summary(Path(os.environ["ENGRAM_DB"]))
    assert "Cold — no surface" not in summary


def test_cold_memories_uses_strict_cutoff(temp_db):
    mid = _insert_mem(temp_db, "edge", created_ago_days=10)
    [m] = memory_store.list_memories(temp_db, order="audit")
    # created exactly at the cutoff is excluded (strict <); one second later, in
    assert agent._cold_memories([m], m.created_ts) == []
    assert [x.id for x in agent._cold_memories([m], m.created_ts + 1)] == [mid]


def test_cold_flags_memory_narrowed_into_silence(temp_db):
    """A memory that surfaced 28x and then had its trigger narrowed past every
    real command keeps a fat surface_count and a healthy q forever. Under the old
    `surface_count == 0` predicate it was invisible to every metric."""
    silent = _insert_mem(temp_db, "narrowed-into-silence", created_ago_days=90,
                         surface_count=28, last_surfaced_ago_days=40)
    _insert_mem(temp_db, "still-firing", created_ago_days=90,
                surface_count=5, last_surfaced_ago_days=1)
    never = _insert_mem(temp_db, "never-surfaced", created_ago_days=90)

    # still-firing is excluded; went-silent sorts FIRST so it survives truncation
    assert _cold_ids(temp_db, days=30) == [silent, never]
    cold = _summary(temp_db).split("Cold — no surface", 1)[1]
    assert "WENT SILENT after 28 surfaces" in cold
    assert "widen it back, do NOT archive" in cold


# ---------- enriched memory summary (WS4.4 / WS5.1) ----------


def _summary(temp_db):
    return agent._get_memory_summary(Path(os.environ["ENGRAM_DB"]))


def _set_counters(conn, mid, *, useful, noise):
    conn.execute("UPDATE memories SET useful_count=?, noise_count=?, surface_count=? "
                 "WHERE id=?", (useful, noise, useful + noise, mid))
    conn.commit()


def test_append_bounded_truncates_past_budget():
    # The per-section budget guard (MAX_SUMMARY_SECTION_CHARS) keeps one enriched
    # section from crowding the transcripts out of the agent's context.
    lines: list = []
    items = ["x" * 100 for _ in range(10)]
    agent._append_bounded(lines, items, lambda s: s, budget=250)
    assert lines[0] == items[0]                       # always shows >=1 item
    rendered = [ln for ln in lines if ln == items[0]]
    assert len(rendered) < len(items)                 # stopped before the end
    assert lines[-1] == "  ... (8 more omitted for budget)"


def test_append_bounded_shows_all_within_budget():
    lines: list = []
    agent._append_bounded(lines, ["a", "b", "c"], lambda s: s, budget=1000)
    assert lines == ["a", "b", "c"]                    # no omitted marker


def test_summary_flags_narrow_or_archive_candidate(temp_db):
    good = _insert_mem(temp_db, "solid", created_ago_days=1, surface_count=5)
    _set_counters(temp_db, good, useful=5, noise=0)
    bad = _insert_mem(temp_db, "over-matcher", created_ago_days=1)
    _set_counters(temp_db, bad, useful=1, noise=6)   # q<0.5, noise-dominant
    memory_store.add_token_trigger(temp_db, bad, ["docker", "build"])
    temp_db.commit()

    summary = _summary(temp_db)
    section = summary.split("Narrow-or-archive candidates", 1)
    assert len(section) == 2, "flagged section missing"
    assert f'[{bad}] "over-matcher"' in section[1]
    assert f'[{good}]' not in section[1]         # healthy memory not flagged
    assert "[docker build]" in section[1]         # trigger rendered for triage


def test_summary_dup_clusters_group_shared_trigger(temp_db):
    a = _insert_mem(temp_db, "mem-a", created_ago_days=1, surface_count=1)
    b = _insert_mem(temp_db, "mem-b", created_ago_days=1, surface_count=1)
    c = _insert_mem(temp_db, "mem-c", created_ago_days=1, surface_count=1)
    memory_store.add_token_trigger(temp_db, a, ["git", "push"])
    memory_store.add_token_trigger(temp_db, b, ["git", "push"])   # dup of a
    memory_store.add_token_trigger(temp_db, c, ["npm", "test"])   # alone
    temp_db.commit()

    summary = _summary(temp_db)
    section = summary.split("Duplicate trigger clusters", 1)
    assert len(section) == 2, "dup-cluster section missing"
    assert "tokens {git, push}" in section[1]
    assert f'[{a}] "mem-a"' in section[1] and f'[{b}] "mem-b"' in section[1]
    assert "npm" not in section[1]                # unshared trigger not a cluster


def test_summary_inventory_shows_unused_split(temp_db):
    mid = _insert_mem(temp_db, "situational", created_ago_days=1, surface_count=2)
    session_state.log_surfaces(temp_db, "sess-1", [mid], None, "PreToolUse", 1,
                               int(time.time()))
    temp_db.execute("UPDATE session_surfaces SET outcome='unused' WHERE memory_id=?", (mid,))
    temp_db.commit()

    summary = _summary(temp_db)
    assert "unused=1" in summary


def test_summary_cold_includes_body_and_triggers(temp_db):
    mid = _insert_mem(temp_db, "cold-detailed", created_ago_days=40)
    memory_store.add_path_trigger(temp_db, mid, "infra/**/Dockerfile")
    temp_db.commit()

    summary = _summary(temp_db)
    cold = summary.split("Cold — no surface", 1)[1]
    assert "path:infra/**/Dockerfile" in cold      # trigger list inline
    assert "body of cold-detailed" in cold          # body snippet inline


def test_summary_injects_open_recommendation_backlog(temp_db):
    runs.record_run(
        temp_db, run_date="2026-07-01", started_ts=1, completed_ts=2,
        sessions_scanned=1, episodes_evaluated=0, memories_weakened=0,
        memories_archived=0, memories_discovered=0, report="r", quality_score=0.5,
        surfaces_helpful=0, surfaces_noise=0, memories_verified=0)
    runs.insert_recommendations(
        temp_db, "2026-07-01",
        [{"title": "path-glob read noise", "severity": "warn", "status": "open",
          "detail": "keeps recurring", "issue_url": None}],
        now_ts=1000)

    summary = _summary(temp_db)
    section = summary.split("Standing recommendation backlog", 1)
    assert len(section) == 2, "backlog section missing"
    assert '"path-glob read noise"' in section[1]
    assert "keeps recurring" in section[1]
