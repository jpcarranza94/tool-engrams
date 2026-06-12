"""engram CLI entrypoint — ToolEngrams command-line interface.

Wires hook handlers (pretool, session-start, user-prompt, post-tool)
plus seed and all formation subcommands (remember, forget, pin, recall).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable

from . import pause, watcher
from .cli import (
    consolidate,
    dashboard,
    doctor,
    edit,
    forget,
    judge,
    mark_noise,
    migrate_v1_to_v2,
    monitor,
    pin,
    quarantine,
    rebuild_triggers,
    recall,
    remember,
    resolve_slug,
    seed,
    skip,
    status,
    trigger,
    verify,
)
from .hooks import (
    flush,
    post_tool,
    post_tool_failure,
    pretool,
    session_start,
    stop,
    user_prompt,
)

# Subcommands that own their own argparse (they accept flags that
# conflict with the top-level parser). Listed here so the dispatch
# logic can forward argv cleanly.
_SELF_PARSING = {
    "remember": remember.main,
    "forget": forget.main,
    "verify": verify.main,
    "skip": skip.main,
    "judge": judge.main,
    "trigger": trigger.main,
    "mark-noise": mark_noise.main,
    "resolve-slug": resolve_slug.main,
    "pin": pin.main,
    "recall": recall.main,
    "consolidate": consolidate.main,
    "edit": edit.main,
    "quarantine": quarantine.main,
    "status": status.main,
    "doctor": doctor.main,
    "seed": seed.main,
    "dashboard": dashboard.main,
    "watcher-tick": watcher.tick.main,
    "monitor": monitor.main,
    "migrate-v1-to-v2": migrate_v1_to_v2.main,
    "rebuild-triggers": rebuild_triggers.main,
}


# Hook handlers stay reachable under $ENGRAM_ALLOWED_VERBS: they fire inside
# engine sandbox sessions (no --bare), are fail-open by contract, and already
# self-skip there via the ENGRAM_IN_WATCHER recursion guard — denying them
# here would surface as hook errors inside every watcher session.
_HOOK_COMMANDS = frozenset({
    "pretool", "session-start", "post-tool", "post-tool-failure",
    "user-prompt", "stop", "flush",
})


def _verb_guard(raw: list[str]) -> int | None:
    """Engine-agnostic containment backstop: when $ENGRAM_ALLOWED_VERBS is set
    (watcher children: `remember` / `judge,quarantine`), refuse any other
    subcommand at dispatch — even an engine whose native sandbox can't express
    a per-command allowlist still can't reach `engram forget` through us."""
    allowed = os.environ.get("ENGRAM_ALLOWED_VERBS", "")
    if not allowed or not raw:
        return None
    verb = raw[0]
    if verb in _HOOK_COMMANDS or verb in ("-h", "--help"):
        return None
    verbs = {v.strip() for v in allowed.split(",") if v.strip()}
    if verb in verbs:
        return None
    print(f"engram: subcommand {verb!r} is not permitted in this context "
          f"(ENGRAM_ALLOWED_VERBS={allowed})", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    denied = _verb_guard(raw)
    if denied is not None:
        return denied

    # Fast-path: self-parsing subcommands get forwarded directly.
    if raw and raw[0] in _SELF_PARSING:
        return _SELF_PARSING[raw[0]](raw[1:])

    parser = argparse.ArgumentParser(prog="engram")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pretool", help="PreToolUse hook handler (reads JSON on stdin)")
    sub.add_parser("session-start", help="SessionStart hook handler (reads JSON on stdin)")
    sub.add_parser("post-tool", help="PostToolUse hook handler — success reinforcement (reads JSON on stdin)")
    sub.add_parser("post-tool-failure", help="PostToolUseFailure hook handler — hint injection + arms the watcher (reads JSON on stdin)")
    sub.add_parser("user-prompt", help="UserPromptSubmit hook handler — fires a watcher tick on a likely correction (reads JSON on stdin)")
    sub.add_parser("stop", help="Stop hook handler — primary event-driven watcher trigger (reads JSON on stdin)")
    sub.add_parser("flush", help="SessionEnd/PreCompact hook handler — final watcher flush tick (reads JSON on stdin)")
    sub.add_parser("seed", help="Insert example memories for smoke testing "
                                "(--with-block, --remove)", add_help=False)
    sub.add_parser("cleanup", help="Reap cold watcher residue: dead watcher_state rows, stale sandboxes, internal transcripts")
    sub.add_parser("pause", help="Kill switch: stop all surfacing, watcher ticks, and background spend")
    sub.add_parser("resume", help="Undo 'engram pause' — turn the memory system back on")

    # Listed so --help shows them, but dispatch goes through _SELF_PARSING above.
    sub.add_parser("remember", help="Extract triggers from body text and insert a memory", add_help=False)
    sub.add_parser("forget", help="Soft-demote or archive a memory", add_help=False)
    sub.add_parser("edit", help="In-place content correction — preserves id, counters, "
                                "surfaces, triggers", add_help=False)
    sub.add_parser("quarantine", help="Pull a harmful memory out of circulation, "
                                      "reversibly (eval watcher's emergency brake)",
                   add_help=False)
    sub.add_parser("verify", help="Mark a memory as still accurate (last_verified_ts = now)", add_help=False)
    sub.add_parser("skip", help="Mark the most recent surface of a memory as unused (negative signal)", add_help=False)
    sub.add_parser("judge", help="Evaluation watcher verb: label a surfaced memory helpful|unused|noise", add_help=False)
    sub.add_parser("trigger", help="Add/remove/list triggers on a memory (trigger-narrowing, preserves counters)", add_help=False)
    sub.add_parser("mark-noise", help="Retroactively mark unmarked surfaces of a memory as noise", add_help=False)
    sub.add_parser("resolve-slug", help="Reverse a Claude Code project slug to candidate paths", add_help=False)
    sub.add_parser("pin", help="Pin/unpin a memory", add_help=False)
    sub.add_parser("recall", help="Browse and search the memory store", add_help=False)
    sub.add_parser("consolidate", help="Nightly consolidation — replay and prune", add_help=False)
    sub.add_parser("status", help="Memory health (human on tty, JSON when piped or --json)", add_help=False)
    sub.add_parser("doctor", help="Wiring + liveness diagnostics (hooks, PATH, claude version, DB, activity)", add_help=False)
    sub.add_parser("dashboard", help="Open HTML dashboard in browser")
    sub.add_parser("monitor", help="Resource usage and watcher activity", add_help=False)
    sub.add_parser("migrate-v1-to-v2", help="One-shot migration from a v1-era DB to the v2 schema", add_help=False)
    sub.add_parser("rebuild-triggers", help="Re-extract triggers from memory bodies (post-migration repair)", add_help=False)

    args = parser.parse_args(argv)

    handlers: dict[str, Callable[[], int]] = {
        "pretool": pretool.main,
        "session-start": session_start.main,
        "post-tool": post_tool.main,
        "post-tool-failure": post_tool_failure.main,
        "user-prompt": user_prompt.main,
        "stop": stop.main,
        "flush": flush.main,
        "cleanup": watcher.cleanup.run_cleanup,
        "pause": pause.run_pause,
        "resume": pause.run_resume,
    }

    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
