"""Stateless formation context injection (ADR-0005): the session-saves list and
the prior-delta tail re-supply the two useful bits of cross-tick state."""

from __future__ import annotations

import json
import time

from toolengrams import memory_store
from toolengrams.utils import project_slug_for_cwd
from toolengrams.watcher import runs_store, tick


def _bash_line(cmd: str) -> str:
    return json.dumps({
        "type": "message",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}
        ]},
    }) + "\n"


def _run(conn, session="s", role="formation", status="ok",
         cursor_from=0, cursor_to=None) -> int:
    rid = runs_store.start_run(
        conn, work_session_id=session, role=role, pid=1,
        started_ts=int(time.time()), model="sonnet", flush=False,
        cursor_from=cursor_from, cwd="/cwd",
    )
    conn.execute("UPDATE watcher_runs SET status = ?, cursor_to = ? WHERE id = ?",
                 (status, cursor_to, rid))
    return rid


def test_session_saves_section_lists_created_memories(temp_db):
    rid = _run(temp_db)
    runs_store.record_event(temp_db, run_id=rid, ts=int(time.time()),
                            kind="created", memory_id=7, memory_name="gh merge lore")
    section = tick._session_saves_section("s")
    assert "Already saved this session" in section
    assert "[id=7] gh merge lore" in section
    assert "MERGES" in section  # the merge instruction rides along


def test_session_saves_section_empty_without_saves(temp_db):
    assert tick._session_saves_section("nope") == ""


def test_prior_tail_section_reads_previous_window(temp_db, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("earlier failing command") +
                          _bash_line("current window command"))
    # Prior ok run consumed line 0..1; current tick's cursor sits at 1.
    _run(temp_db, cursor_from=0, cursor_to=1)

    section = tick._prior_tail_section("s", str(transcript), cursor=1)
    assert "Recent prior activity" in section
    assert "earlier failing command" in section
    assert "current window command" not in section  # only the PRIOR window
    assert "already considered" in section          # don't re-save framing


def test_prior_tail_absent_without_prior_run(temp_db, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_bash_line("x"))
    assert tick._prior_tail_section("s", str(transcript), cursor=0) == ""
    assert tick._prior_tail_section("s", str(transcript), cursor=1) == ""


def test_prior_tail_is_capped(temp_db, tmp_path):
    transcript = tmp_path / "t.jsonl"
    big = "x" * 2000
    transcript.write_text("".join(_bash_line(f"{big} {i}") for i in range(5)))
    _run(temp_db, cursor_from=0, cursor_to=5)

    section = tick._prior_tail_section("s", str(transcript), cursor=5)
    assert section  # present
    assert len(section) < tick.PRIOR_TAIL_MAX_CHARS + 400  # header + cap


def test_formation_message_carries_full_prompt_every_tick(temp_db, tmp_path):
    """No resumed-session header anymore: each tick is fresh and self-contained."""
    decision = tick._formation_decision(
        "s", "/cwd", 'TOOL (Bash): x\nRESULT: ok', 1, flush=False, armed=False,
        transcript_path=str(tmp_path / "t.jsonl"), cursor=0,
    )
    assert decision.skip is False
    # The full formation prompt (not a "--- New activity ---" header) leads.
    assert "memory" in decision.message.lower()
    assert decision.message.startswith("--- New activity ---") is False


# ---------- command anchor (real commands this window) ----------


def test_command_anchor_section_extracts_dedups_and_labels():
    delta = (
        'TOOL (Bash): git status\n'
        'RESULT: clean\n'
        'TOOL (Bash): git status\n'
        'TOOL (Bash): gh pr view 123 --json state\n'
    )
    section = tick._command_anchor_section(delta)
    assert "Real commands this window" in section
    assert "bind triggers to THESE" in section
    assert section.count("git status") == 1                   # deduped
    assert "gh pr view 123 --json state" in section


def test_command_anchor_section_includes_non_bash_tool_lines():
    delta = 'TOOL (Read): /repo/file.py\nTOOL (unknown)\n'
    section = tick._command_anchor_section(delta)
    assert "/repo/file.py" in section
    assert "TOOL (unknown)" not in section   # no colon → nothing to anchor on


def test_command_anchor_section_empty_without_commands():
    assert tick._command_anchor_section("") == ""
    assert tick._command_anchor_section('USER: "hi"\nAGENT: "ok"\n') == ""


def test_command_anchor_section_caps_count():
    delta = "".join(f"TOOL (Bash): make target-{i}\n" for i in range(30))
    section = tick._command_anchor_section(delta)
    listed = [l for l in section.splitlines() if l.startswith("- ")]
    assert len(listed) == tick.COMMAND_ANCHOR_MAX


def test_command_anchor_section_caps_chars():
    delta = "".join(f"TOOL (Bash): {'x' * 300} {i}\n" for i in range(20))
    section = tick._command_anchor_section(delta)
    listed = [l for l in section.splitlines() if l.startswith("- ")]
    assert len(listed) < tick.COMMAND_ANCHOR_MAX          # char cap hit first
    assert len(section) < tick.COMMAND_ANCHOR_MAX_CHARS + 500  # header/bullet overhead


# ---------- outcome feedback (how recent saves fared) ----------


def _seed_memory(conn, name, *, useful=0, noise=0, surfaces=0,
                 created_ts=None, scope="global", project_slug=None,
                 archived_ts=None) -> int:
    ts = created_ts if created_ts is not None else int(time.time())
    cur = conn.execute(
        "INSERT INTO memories (name, description, body, kind, scope, "
        " project_slug, created_ts, surface_count, useful_count, noise_count, "
        " archived_ts) VALUES (?, '', 'body', 'hint', ?, ?, ?, ?, ?, ?, ?)",
        (name, scope, project_slug, ts, surfaces, useful, noise, archived_ts),
    )
    return cur.lastrowid


def _seed_created(conn, memory_id, memory_name, *, session="s", ts=None):
    rid = _run(conn, session=session)
    runs_store.record_event(
        conn, run_id=rid, ts=ts if ts is not None else int(time.time()),
        kind="created", memory_id=memory_id, memory_name=memory_name,
    )


def test_recent_created_memory_ids_windows_and_dedups(temp_db):
    """The runs_store half touches only its own tables: distinct ids of formation
    'created' events inside the lookback window (a `--into` merge logs two events
    for one id → one id back; an out-of-window save drops out)."""
    now = int(time.time())
    in_window = now - 3 * 86400
    out_of_window = now - 20 * 86400

    recent = _seed_memory(temp_db, "recent", created_ts=in_window)
    _seed_created(temp_db, recent, "recent", ts=in_window)

    merged = _seed_memory(temp_db, "merged-twice", created_ts=in_window)
    _seed_created(temp_db, merged, "merged-twice", ts=in_window)
    _seed_created(temp_db, merged, "merged-twice", ts=in_window + 5)  # later --into

    stale = _seed_memory(temp_db, "stale", created_ts=out_of_window)
    _seed_created(temp_db, stale, "stale", ts=out_of_window)

    ids = runs_store.recent_created_memory_ids(temp_db, since_ts=now - 10 * 86400)
    assert sorted(ids) == sorted([recent, merged])   # deduped, stale excluded


def test_save_outcomes_filters_scope_and_archived(temp_db):
    """The memory_store half applies the scope + non-archived filter over a set
    of ids (empty id list → no query)."""
    now = int(time.time())
    keep_global = _seed_memory(temp_db, "keep-global", scope="global")
    keep_project = _seed_memory(temp_db, "keep-project", scope="project",
                                project_slug="-my-project")
    other_project = _seed_memory(temp_db, "other-project", scope="project",
                                 project_slug="-other-project")
    archived = _seed_memory(temp_db, "archived", scope="global", archived_ts=now)
    ids = [keep_global, keep_project, other_project, archived]

    rows = memory_store.save_outcomes(temp_db, ids, project_slug="-my-project")
    assert {r["name"] for r in rows} == {"keep-global", "keep-project"}
    assert memory_store.save_outcomes(temp_db, [], project_slug="-my-project") == []


def test_formation_feedback_section_classifies_and_bounds(temp_db, tmp_path):
    cwd = str(tmp_path)
    project_slug = project_slug_for_cwd(cwd, use_git=True)
    now = int(time.time())
    recent = now - 3 * 86400

    noisy = _seed_memory(temp_db, "gh-pr-view-json", useful=0, noise=4,
                         surfaces=6, created_ts=recent, scope="global")
    _seed_created(temp_db, noisy, "gh-pr-view-json", ts=recent)

    cold = _seed_memory(temp_db, "ergdb-pto-schema", useful=0, noise=0,
                        surfaces=0, created_ts=now - 9 * 86400, scope="global")
    _seed_created(temp_db, cold, "ergdb-pto-schema", ts=now - 9 * 86400)

    good = _seed_memory(temp_db, "jira-move-closing-comment", useful=9, noise=1,
                        surfaces=10, created_ts=recent, scope="project",
                        project_slug=project_slug)
    _seed_created(temp_db, good, "jira-move-closing-comment", ts=recent)

    # Too young to count as COLD yet (0 surfaces, but under the 2-day grace
    # period) — must show up in neither the bad nor the good list.
    fresh = _seed_memory(temp_db, "too-new-to-judge", useful=0, noise=0,
                         surfaces=0, created_ts=now - 3600, scope="global")
    _seed_created(temp_db, fresh, "too-new-to-judge", ts=now - 3600)

    section = tick._formation_feedback_section(cwd)

    assert "How your recent saves fared" in section
    assert "gh-pr-view-json" in section
    assert "trigger over-matched" in section
    assert "ergdb-pto-schema" in section
    assert "trigger never fired" in section
    assert "Good (keep doing this)" in section
    assert "jira-move-closing-comment" in section
    assert "too-new-to-judge" not in section


def test_formation_feedback_section_empty_without_history(temp_db, tmp_path):
    assert tick._formation_feedback_section(str(tmp_path)) == ""


def test_formation_feedback_section_caps_bad_and_good(temp_db, tmp_path):
    cwd = str(tmp_path)
    now = int(time.time())
    recent = now - 1 * 86400
    for i in range(tick.FEEDBACK_MAX_BAD + 3):
        mid = _seed_memory(temp_db, f"noisy-{i}", useful=0, noise=3, surfaces=3,
                           created_ts=recent, scope="global")
        _seed_created(temp_db, mid, f"noisy-{i}", ts=recent)
    for i in range(tick.FEEDBACK_MAX_GOOD + 3):
        mid = _seed_memory(temp_db, f"good-{i}", useful=5, noise=0, surfaces=5,
                           created_ts=recent, scope="global")
        _seed_created(temp_db, mid, f"good-{i}", ts=recent)

    section = tick._formation_feedback_section(cwd)
    bad_lines = [l for l in section.splitlines() if l.startswith("- 'noisy-")]
    good_lines = [l for l in section.splitlines() if l.startswith("- 'good-")]
    assert len(bad_lines) == tick.FEEDBACK_MAX_BAD
    assert len(good_lines) == tick.FEEDBACK_MAX_GOOD


# ---------- both sections ride the fresh formation message ----------


def test_formation_message_includes_command_anchor_and_feedback(temp_db, tmp_path):
    cwd = str(tmp_path)
    now = int(time.time())
    noisy = _seed_memory(temp_db, "gh-pr-view-json", useful=0, noise=4,
                         surfaces=6, created_ts=now - 86400, scope="global")
    _seed_created(temp_db, noisy, "gh-pr-view-json", ts=now - 86400)

    delta = 'TOOL (Bash): gh pr view 123 --json state\nRESULT: ok\n'
    decision = tick._formation_decision(
        "s", cwd, delta, 1, flush=False, armed=False,
        transcript_path=str(tmp_path / "t.jsonl"), cursor=0,
    )

    assert decision.skip is False
    assert "Real commands this window" in decision.message
    assert "gh pr view 123 --json state" in decision.message
    assert "How your recent saves fared" in decision.message
    assert "gh-pr-view-json" in decision.message
