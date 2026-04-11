"""memctl CLI entrypoint.

v1 wires the four hook handlers (pretool, session-start, user-prompt,
post-failure) plus `seed`. The formation subcommands (remember, forget,
pin, recall, export) are stubs until v1.5.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from .commands import post_failure, pretool, seed, session_start, user_prompt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memctl")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pretool", help="PreToolUse hook handler (reads JSON on stdin)")
    sub.add_parser("session-start", help="SessionStart hook handler (reads JSON on stdin)")
    sub.add_parser("user-prompt", help="UserPromptSubmit hook handler (reads JSON on stdin)")
    sub.add_parser("post-failure", help="PostToolUse hook handler — failure subset (reads JSON on stdin)")
    sub.add_parser("seed", help="Insert example memories for smoke testing")

    remember = sub.add_parser("remember", help="Formation: extract triggers + insert (stub)")
    remember.add_argument("text", nargs="?", default=None)

    forget = sub.add_parser("forget", help="Soft demote a memory (stub)")
    forget.add_argument("name", nargs="?", default=None)
    forget.add_argument("--delete", action="store_true")

    pin = sub.add_parser("pin", help="Pin a memory so reinforcement doesn't gate it (stub)")
    pin.add_argument("name", nargs="?", default=None)

    recall = sub.add_parser("recall", help="Deep-browse memories (stub)")
    recall.add_argument("query", nargs="?", default=None)

    sub.add_parser("export", help="Dump memories to markdown (stub)")

    args = parser.parse_args(argv)

    handlers: dict[str, Callable[[], int]] = {
        "pretool": pretool.main,
        "session-start": session_start.main,
        "user-prompt": user_prompt.main,
        "post-failure": post_failure.main,
        "seed": seed.main,
        "remember": _stub_unimpl("remember"),
        "forget": _stub_unimpl("forget"),
        "pin": _stub_unimpl("pin"),
        "recall": _stub_unimpl("recall"),
        "export": _stub_unimpl("export"),
    }

    return handlers[args.command]()


def _stub_unimpl(name: str) -> Callable[[], int]:
    def _run() -> int:
        print(f"memctl {name}: not yet implemented (v1.5)", file=sys.stderr)
        return 0

    return _run


if __name__ == "__main__":
    raise SystemExit(main())
