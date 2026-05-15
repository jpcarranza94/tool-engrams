"""engram CLI entrypoint — ToolEngrams command-line interface.

Wires hook handlers (pretool, session-start, user-prompt, post-tool)
plus seed and all formation subcommands (remember, forget, pin, recall).
Export is the only remaining stub.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from . import watcher
from .cli import (
    consolidate,
    dashboard,
    forget,
    migrate_v1_to_v2,
    monitor,
    pin,
    rebuild_triggers,
    recall,
    remember,
    resolve_slug,
    seed,
    skip,
    status,
    verify,
)
from .hooks import (
    post_tool,
    post_tool_failure,
    pretool,
    session_start,
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
    "resolve-slug": resolve_slug.main,
    "pin": pin.main,
    "recall": recall.main,
    "consolidate": consolidate.main,
    "status": status.main,
    "dashboard": dashboard.main,
    "watcher": watcher.main,
    "monitor": monitor.main,
    "migrate-v1-to-v2": migrate_v1_to_v2.main,
    "rebuild-triggers": rebuild_triggers.main,
}


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # Fast-path: self-parsing subcommands get forwarded directly.
    if raw and raw[0] in _SELF_PARSING:
        return _SELF_PARSING[raw[0]](raw[1:])

    parser = argparse.ArgumentParser(prog="engram")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pretool", help="PreToolUse hook handler (reads JSON on stdin)")
    sub.add_parser("session-start", help="SessionStart hook handler (reads JSON on stdin)")
    sub.add_parser("post-tool", help="PostToolUse hook handler — success reinforcement (reads JSON on stdin)")
    sub.add_parser("post-tool-failure", help="PostToolUseFailure hook handler — hint injection on tool failure (reads JSON on stdin)")
    sub.add_parser("user-prompt", help="UserPromptSubmit hook handler — watcher liveness check (reads JSON on stdin)")
    sub.add_parser("seed", help="Insert example memories for smoke testing")

    # Listed so --help shows them, but dispatch goes through _SELF_PARSING above.
    sub.add_parser("remember", help="Extract triggers from body text and insert a memory", add_help=False)
    sub.add_parser("forget", help="Soft-demote or archive a memory", add_help=False)
    sub.add_parser("verify", help="Mark a memory as still accurate (last_verified_ts = now)", add_help=False)
    sub.add_parser("skip", help="Mark the most recent surface of a memory as unused (negative signal)", add_help=False)
    sub.add_parser("resolve-slug", help="Reverse a Claude Code project slug to candidate paths", add_help=False)
    sub.add_parser("pin", help="Pin/unpin a memory", add_help=False)
    sub.add_parser("recall", help="Browse and search the memory store", add_help=False)
    sub.add_parser("consolidate", help="Nightly consolidation — replay and prune", add_help=False)
    sub.add_parser("status", help="Memory health JSON", add_help=False)
    sub.add_parser("dashboard", help="Open HTML dashboard in browser")
    sub.add_parser("watcher", help="Persistent parallel watcher (background, model via $ENGRAM_WATCHER_MODEL)", add_help=False)
    sub.add_parser("monitor", help="Resource usage and watcher activity", add_help=False)
    sub.add_parser("migrate-v1-to-v2", help="One-shot migration from v1 (design-v8) to v2 (design-v9)", add_help=False)
    sub.add_parser("rebuild-triggers", help="Re-extract triggers from memory bodies (post-migration repair)", add_help=False)

    sub.add_parser("export", help="Dump memories to markdown (stub)")

    args = parser.parse_args(argv)

    handlers: dict[str, Callable[[], int]] = {
        "pretool": pretool.main,
        "session-start": session_start.main,
        "post-tool": post_tool.main,
        "post-tool-failure": post_tool_failure.main,
        "user-prompt": user_prompt.main,
        "seed": seed.main,
        "export": _stub_unimpl("export"),
    }

    return handlers[args.command]()


def _stub_unimpl(name: str) -> Callable[[], int]:
    def _run() -> int:
        print(f"engram {name}: not yet implemented (v1.5)", file=sys.stderr)
        return 0

    return _run


if __name__ == "__main__":
    raise SystemExit(main())
