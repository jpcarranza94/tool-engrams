"""memctl CLI entrypoint.

v1 wires `pretool` and `seed` for real. Other subcommands are stubs that
exit 0 with an empty hook output — enough to wire every hook event without
breaking anything while the formation/reinforcement layers are built out.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Callable

from .commands import pretool, seed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memctl")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("pretool", help="PreToolUse hook handler (reads JSON on stdin)")
    sub.add_parser("session-start", help="SessionStart hook handler (stub)")
    sub.add_parser("user-prompt", help="UserPromptSubmit hook handler (stub)")
    sub.add_parser("post-failure", help="PostToolUseFailure hook handler (stub)")
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
        "seed": seed.main,
        "session-start": _stub_hook("SessionStart"),
        "user-prompt": _stub_hook("UserPromptSubmit"),
        "post-failure": _stub_hook("PostToolUseFailure"),
        "remember": _stub_unimpl("remember"),
        "forget": _stub_unimpl("forget"),
        "pin": _stub_unimpl("pin"),
        "recall": _stub_unimpl("recall"),
        "export": _stub_unimpl("export"),
    }

    return handlers[args.command]()


def _stub_hook(event_name: str) -> Callable[[], int]:
    """Stub that drains stdin and emits an empty hook output. Keeps the hook
    pipeline happy while the real handler isn't built yet."""

    def _run() -> int:
        try:
            sys.stdin.read()
        except Exception:
            pass
        sys.stdout.write(json.dumps({}))
        sys.stdout.write("\n")
        return 0

    return _run


def _stub_unimpl(name: str) -> Callable[[], int]:
    def _run() -> int:
        print(f"memctl {name}: not yet implemented (v1.5)", file=sys.stderr)
        return 0

    return _run


if __name__ == "__main__":
    raise SystemExit(main())
