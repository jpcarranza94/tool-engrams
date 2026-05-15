"""`engram resolve-slug <slug>` — reverse Claude Code's project slug to a real path.

Used by the consolidation agent's staleness audit (consolidation.md Task 5).
The slug `-Users-jpcar-projects-agent-service` maps to a path, but the mapping
is lossy when directory names contain `-` (e.g. `tool-engrams` splits as
`tool/engrams` under naive replacement). This CLI enumerates every plausible
reading, keeps the ones that exist on disk, and prints them deepest-first.

Note: slugs start with `-` which argparse would treat as a flag. We bypass
argparse for the positional and just read argv directly — single-arg CLI.
"""

from __future__ import annotations

import json
import sys

from ..utils import unslugify_candidates


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    # Allow `-h`/`--help` even though we don't use argparse.
    if not raw or raw[0] in ("-h", "--help"):
        sys.stderr.write("usage: engram resolve-slug <slug>\n")
        return 0 if raw and raw[0] in ("-h", "--help") else 2

    slug = raw[0]
    candidates = unslugify_candidates(slug)
    if not candidates:
        print(json.dumps({
            "slug": slug,
            "candidates": [],
            "best": None,
            "note": "no existing path matches this slug",
        }))
        return 1

    paths = [str(p) for p in candidates]
    print(json.dumps({
        "slug": slug,
        "candidates": paths,
        "best": paths[0],
    }))
    return 0
