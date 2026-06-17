"""`engram config` — show / get / set / unset the durable config file.

Writes ``<engram home>/config.json`` so settings survive across sessions and
launchd/cron's minimal env. `set` validates the key against `config.SPEC` and
coerces the value, so a typo'd key errors loudly instead of silently doing
nothing. See `toolengrams/config.py` for the schema and precedence.
"""

from __future__ import annotations

import argparse
import json
import sys

from .. import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engram config")
    sub = parser.add_subparsers(dest="action", required=True)

    show = sub.add_parser("show", help="Show every key: file value, env override, effective value.")
    show.add_argument("--json", action="store_true", help="Machine-readable output.")
    g = sub.add_parser("get", help="Print one key's file value.")
    g.add_argument("key")
    s = sub.add_parser("set", help="Set a key in the file (validated + coerced).")
    s.add_argument("key")
    s.add_argument("value")
    u = sub.add_parser("unset", help="Remove a key from the file.")
    u.add_argument("key")
    sub.add_parser("keys", help="List every settable key.")

    args = parser.parse_args(argv)

    if args.action == "show":
        return _show(json_out=args.json)
    if args.action == "keys":
        for key in config.known_keys():
            print(f"{key}  ({config.env_for(key)})")
        return 0
    if args.action == "get":
        value = config.get(args.key)
        if value is None:
            print(f"engram config: {args.key} is not set", file=sys.stderr)
            return 1
        print(value)
        return 0
    if args.action == "set":
        try:
            stored = config.set_value(args.key, args.value)
        except KeyError:
            return _unknown_key(args.key)
        except ValueError as e:
            print(f"engram config: invalid value for {args.key}: {e}", file=sys.stderr)
            return 2
        print(json.dumps({"set": args.key, "value": stored,
                          "env": config.env_for(args.key),
                          "path": str(config.config_path())}))
        return 0
    if args.action == "unset":
        try:
            removed = config.unset(args.key)
        except KeyError:
            return _unknown_key(args.key)
        print(json.dumps({"unset": args.key, "was_set": removed}))
        return 0
    return 0


def _unknown_key(key: str) -> int:
    print(f"engram config: unknown key {key!r}. See `engram config keys`.",
          file=sys.stderr)
    return 2


def _show(*, json_out: bool) -> int:
    rows = config.effective()
    if json_out:
        print(json.dumps({"path": str(config.config_path()), "settings": rows}))
        return 0
    print(f"config: {config.config_path()}")
    width = max(len(r["key"]) for r in rows)
    for r in rows:
        eff = "—" if r["effective"] is None else r["effective"]
        tag = "" if r["source"] == "default" else f"  [{r['source']}]"
        print(f"  {r['key']:<{width}}  {eff}{tag}")
    return 0
