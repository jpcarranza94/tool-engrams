"""$ENGRAM_ALLOWED_VERBS — the engine-agnostic containment backstop at CLI
dispatch (see __main__._verb_guard)."""

from __future__ import annotations

from toolengrams.__main__ import _verb_guard


def test_no_env_means_no_guard(monkeypatch):
    monkeypatch.delenv("ENGRAM_ALLOWED_VERBS", raising=False)
    assert _verb_guard(["forget", "x"]) is None


def test_allowed_verb_passes(monkeypatch):
    monkeypatch.setenv("ENGRAM_ALLOWED_VERBS", "remember")
    assert _verb_guard(["remember", "body"]) is None


def test_disallowed_verb_denied(monkeypatch, capsys):
    monkeypatch.setenv("ENGRAM_ALLOWED_VERBS", "remember")
    assert _verb_guard(["forget", "x"]) == 2
    assert "not permitted" in capsys.readouterr().err


def test_multi_verb_list(monkeypatch):
    monkeypatch.setenv("ENGRAM_ALLOWED_VERBS", "judge,quarantine")
    assert _verb_guard(["judge", "--session-id", "s"]) is None
    assert _verb_guard(["quarantine", "3"]) is None
    assert _verb_guard(["edit", "3"]) == 2


def test_hook_commands_always_pass(monkeypatch):
    """Hooks fire inside engine sandbox sessions and are fail-open by
    contract — the guard must never turn them into exit-2 hook errors."""
    monkeypatch.setenv("ENGRAM_ALLOWED_VERBS", "remember")
    for cmd in ("pretool", "session-start", "post-tool", "post-tool-failure",
                "user-prompt", "stop", "flush"):
        assert _verb_guard([cmd]) is None


def test_help_passes(monkeypatch):
    monkeypatch.setenv("ENGRAM_ALLOWED_VERBS", "remember")
    assert _verb_guard(["--help"]) is None
    assert _verb_guard([]) is None
