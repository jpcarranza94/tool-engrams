"""engram monitor — a live dashboard over the watchers.

Three panes, auto-refreshing: **active now** (runs executing this moment),
**last 24h** (run history), and the **decision stream** (memories created by
formation vs. judged by eval). Backed by the `watcher_runs` / `watcher_run_events`
tables (the `runs_store` seam).

`rich` renders the live view (ADR-0003) and is imported **lazily**, only when the
live loop runs — so importing this module, and the hot path, stay stdlib-only.
When stdout is not a TTY (piped / cron) the dashboard can't run a live loop, so
it prints a one-shot JSON snapshot instead, keeping the view scriptable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .. import db
from ..watcher import runs_store

# A `running` row older than this (with a dead/unknown pid) is shown as stale,
# not active — covers a tick that died before finalizing. ~claude -p timeout + margin.
STALE_AFTER_SEC = 180
DEFAULT_INTERVAL = 2.0
_DAY = 86_400


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    live = sys.stdout.isatty() and not args.json
    if not live:
        with db.session() as conn:
            print(json.dumps(build_snapshot(conn, int(time.time())), indent=2))
        return 0
    return _run_live(args.interval)


# ---------- pure data layer (testable headless) ----------


def _pid_alive(pid: int | None) -> bool:
    """True if a process with this pid currently exists. `PermissionError` means
    it exists but is owned by someone else — still alive."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _short(sid: str | None) -> str:
    return (sid or "")[:8]


def _active_view(row, now_ts: int) -> dict:
    """A `running` row → its display state: active (pid alive + fresh) or stale."""
    age = now_ts - row["started_ts"]
    fresh = age < STALE_AFTER_SEC
    state = "active" if (_pid_alive(row["pid"]) and fresh) else "stale"
    return {
        "session": _short(row["work_session_id"]),
        "role": row["role"],
        "state": state,
        "age_sec": age,
        "pid": row["pid"],
        "cwd": row["cwd"],
    }


def _run_view(row, now_ts: int) -> dict:
    end = row["ended_ts"] if row["ended_ts"] is not None else now_ts
    return {
        "session": _short(row["work_session_id"]),
        "role": row["role"],
        "status": row["status"],
        "started_ts": row["started_ts"],
        "duration_sec": max(0, end - row["started_ts"]),
        "delta_chars": row["delta_chars"],
        "created": row["n_created"],
        "judged": row["n_judged"],
        "error": row["error"],
    }


def _event_view(row) -> dict:
    return {
        "ts": row["ts"],
        "kind": row["kind"],            # created | judged
        "role": row["role"],
        "session": _short(row["work_session_id"]),
        "memory_id": row["memory_id"],
        "memory_name": row["memory_name"],
        "outcome": row["outcome"],      # judged only
    }


def build_snapshot(conn, now_ts: int) -> dict:
    """The full dashboard payload — also the non-TTY JSON output."""
    since = now_ts - _DAY
    return {
        "now": now_ts,
        "active": [_active_view(r, now_ts) for r in runs_store.active_runs(conn)],
        "recent_24h": [_run_view(r, now_ts)
                       for r in runs_store.recent_runs(conn, since, limit=50)],
        "stream": [_event_view(e) for e in runs_store.recent_events(conn, limit=40)],
        "counts_24h": runs_store.counts_since(conn, since),
    }


# ---------- live rich renderer (lazy-imported) ----------


def _run_live(interval: float) -> int:
    try:
        from rich.live import Live
    except ImportError:
        print("engram monitor (live) needs rich: pip install toolengrams[monitor] "
              "(or pip install rich). Use --json for a one-shot snapshot.",
              file=sys.stderr)
        return 1

    try:
        with Live(_render(_load()), refresh_per_second=4, screen=True) as live:
            while True:
                time.sleep(interval)
                live.update(_render(_load()))
    except KeyboardInterrupt:
        pass
    return 0


def _load() -> dict:
    with db.session() as conn:
        return build_snapshot(conn, int(time.time()))


def _render(snap: dict):
    """Build the rich renderable for one snapshot. Imported lazily by callers."""
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    def _ago(ts: int) -> str:
        s = max(0, snap["now"] - ts)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        return f"{s // 3600}h"

    # Active now.
    active = Table(expand=True, box=None)
    for col in ("session", "role", "state", "age", "pid"):
        active.add_column(col)
    for r in snap["active"]:
        color = "bold green" if r["state"] == "active" else "yellow"
        active.add_row(r["session"], r["role"], Text(r["state"], style=color),
                       f'{r["age_sec"]}s', str(r["pid"] or "—"))
    if not snap["active"]:
        active.add_row("—", "no watcher running", "", "", "")

    # Last 24h.
    hist = Table(expand=True, box=None)
    for col in ("when", "session", "role", "status", "dur", "Δchars", "+mem", "judged"):
        hist.add_column(col)
    _status_color = {"ok": "green", "error": "red", "crashed": "red", "running": "cyan"}
    for r in snap["recent_24h"]:
        hist.add_row(
            _ago(r["started_ts"]), r["session"], r["role"],
            Text(r["status"], style=_status_color.get(r["status"], "white")),
            f'{r["duration_sec"]}s', str(r["delta_chars"] or "—"),
            str(r["created"] or ""), str(r["judged"] or ""),
        )

    # Decision stream.
    stream = Table(expand=True, box=None)
    for col in ("when", "what", "memory", "verdict"):
        stream.add_column(col)
    _verdict_color = {"helpful": "green", "noise": "red", "unused": "yellow"}
    for e in snap["stream"]:
        if e["kind"] == "created":
            what = Text("+ created", style="cyan")
            verdict = ""
        else:
            what = Text("judged", style="magenta")
            verdict = Text(e["outcome"] or "?",
                           style=_verdict_color.get(e["outcome"], "white"))
        stream.add_row(_ago(e["ts"]), what,
                       e["memory_name"] or f'#{e["memory_id"]}', verdict)

    c = snap["counts_24h"]
    by = c["runs_by_status"]
    header = (f'runs 24h: ok {by.get("ok", 0)} · error {by.get("error", 0)} · '
              f'crashed {by.get("crashed", 0)} · running {by.get("running", 0)}   '
              f'│ created {c["created"]} · judged {c["judged"]}')

    layout = Layout()
    layout.split_column(
        Layout(Panel(active, title="active watchers", subtitle=header), size=8),
        Layout(Panel(hist, title="runs · last 24h")),
        Layout(Panel(stream, title="decision stream")),
    )
    return layout


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="engram monitor")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"Live refresh seconds (default {DEFAULT_INTERVAL}).")
    parser.add_argument("--json", action="store_true",
                        help="Print a one-shot JSON snapshot and exit (the default "
                             "when stdout is not a TTY).")
    return parser.parse_args(argv)
