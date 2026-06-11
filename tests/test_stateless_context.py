"""Stateless formation context injection (ADR-0005): the session-saves list and
the prior-delta tail re-supply the two useful bits of cross-tick state."""

from __future__ import annotations

import json
import time

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
