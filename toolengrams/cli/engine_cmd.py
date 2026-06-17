"""`engram engine` — show or switch the active background engine.

`engram engine set codex` writes the `engine` key into the config file (the same
durable store install.sh uses), so it takes effect on the next detached watcher
tick / nightly consolidation with no reinstall. It validates the name against
the engine registry and warns — but does not fail — when the engine's binary
isn't on PATH yet, since background selection is fail-open (`selection.py`).

Switching the *engine* (background runner) is a runtime choice. Switching the
*target* (the hooked harness) is not: target hooks are wired into the harness's
own config, and several targets can be wired at once — so wire both and they
coexist; there is nothing to switch. See docs/adr/0012.
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import config
from ..engine import selection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engram engine")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="Print the currently selected engine.")
    s = sub.add_parser("set", help="Switch the active engine (writes the config file).")
    s.add_argument("name")
    sub.add_parser("list", help="List known engines and whether each binary is available.")

    args = parser.parse_args(argv)

    if args.action == "show":
        print(selection.configured_engine_name())
        return 0
    if args.action == "list":
        for name, adapter in selection.ENGINES.items():
            print(f"{name}  ({'available' if adapter.is_available() else 'binary not on PATH'})")
        return 0

    # set
    name = args.name
    if name not in selection.ENGINES:
        print(f"engram engine: unknown engine {name!r}; known: "
              f"{', '.join(selection.ENGINES)}", file=sys.stderr)
        return 2

    config.set_value("engine", name)
    available = selection.ENGINES[name].is_available()
    print(json.dumps({"engine": name, "available": available,
                      "path": str(config.config_path())}))
    if not available:
        print(f"engram engine: warning — {name!r} binary not found on PATH; "
              f"background runs fall back to claude-code until it is installed.",
              file=sys.stderr)
    return 0
