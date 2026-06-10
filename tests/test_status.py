"""engram status — JSON back-compat + human formatting."""

from __future__ import annotations

import json

from toolengrams.cli import status


def test_status_piped_output_stays_json(temp_db, capsys):
    """pytest's captured stdout is not a tty — the default must be JSON so
    existing pipes/scripts keep parsing."""
    assert status.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"kill_switch", "memories", "triggers",
            "last_consolidation", "schedule_installed"} <= payload.keys()


def test_status_json_flag(temp_db, capsys):
    assert status.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["memories"]["active"] == 0


def test_format_human_fresh_db():
    result = {
        "kill_switch": {"disabled": False, "pause_flag": False, "env_override": None},
        "memories": {"active": 3, "archived": 1, "total_surfaces": 7, "total_useful": 2},
        "triggers": {"token_subseq": 4},
        "last_consolidation": None,
        "schedule_installed": False,
    }
    text = status._format_human(result)
    assert "system      active" in text
    assert "3 active, 1 archived" in text
    assert "7 total, 2 judged useful" in text
    assert "4 token, 0 path" in text
    assert "never run (no schedule)" in text


def test_format_human_paused_via_env():
    result = {
        "kill_switch": {"disabled": True, "pause_flag": False, "env_override": "1"},
        "memories": {"active": 0, "archived": 0, "total_surfaces": 0, "total_useful": 0},
        "triggers": {},
        "last_consolidation": {"run_date": "2026-06-09", "sessions_scanned": 17,
                               "memories_archived": 1, "memories_discovered": 1},
        "schedule_installed": True,
    }
    text = status._format_human(result)
    assert "PAUSED via ENGRAM_DISABLED=1" in text
    assert "last run 2026-06-09: 17 sessions scanned" in text
    assert "scheduled nightly" in text
