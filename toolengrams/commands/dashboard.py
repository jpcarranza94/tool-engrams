"""engram dashboard — open a local HTML dashboard in the browser."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from .. import db

LOG_PATH = Path.home() / ".claude" / "tool-engrams" / "observer.log"


def main(argv: list[str] | None = None) -> int:
    conn = db.connect()
    try:
        html = _build_html(conn)
        path = Path(tempfile.gettempdir()) / "engram-dashboard.html"
        path.write_text(html)
        webbrowser.open(f"file://{path}")
        print(f"Dashboard opened: {path}")
        return 0
    finally:
        conn.close()


def _count_observer_processes() -> int:
    """Count running engram observe processes."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "toolengrams.*observe"],
            capture_output=True, text=True, timeout=5,
        )
        return len(result.stdout.strip().splitlines()) if result.stdout.strip() else 0
    except Exception:
        return 0


def _read_observer_stats() -> dict:
    """Parse observer.log for recent activity stats."""
    stats = {"total": 0, "today": 0, "last_entry": "—"}
    try:
        if not LOG_PATH.exists():
            return stats
        lines = LOG_PATH.read_text().splitlines()
        stats["total"] = len(lines)
        today = time.strftime("%Y-%m-%d")
        stats["today"] = sum(1 for l in lines if l.startswith(today))
        if lines:
            stats["last_entry"] = lines[-1][:19]  # timestamp portion
    except Exception:
        pass
    return stats


def _build_html(conn: sqlite3.Connection) -> str:
    memories = conn.execute(
        "SELECT id, name, body, type, scope, project_slug, "
        "surface_count, useful_count, pinned, created_ts, last_surfaced_ts, archived_ts "
        "FROM memories ORDER BY archived_ts IS NOT NULL, id DESC"
    ).fetchall()

    triggers = conn.execute(
        "SELECT memory_id, kind, tool_name, head_joined, path_pattern "
        "FROM triggers ORDER BY memory_id"
    ).fetchall()

    associations = conn.execute(
        "SELECT a.memory_a_id, a.memory_b_id, a.strength, a.co_fire_count, a.last_co_fire_ts, "
        "ma.name AS name_a, mb.name AS name_b "
        "FROM memory_associations a "
        "JOIN memories ma ON ma.id = a.memory_a_id "
        "JOIN memories mb ON mb.id = a.memory_b_id "
        "ORDER BY a.strength DESC"
    ).fetchall()

    surfaces = conn.execute(
        "SELECT ss.session_id, ss.memory_id, m.name, ss.hook, ss.surfaced_ts "
        "FROM session_surfaces ss JOIN memories m ON m.id = ss.memory_id "
        "ORDER BY ss.surfaced_ts DESC LIMIT 50"
    ).fetchall()

    consolidations = conn.execute(
        "SELECT run_date, sessions_scanned, memories_archived, memories_discovered, "
        "memories_strengthened, memories_weakened "
        "FROM consolidation_runs ORDER BY started_ts DESC LIMIT 10"
    ).fetchall()

    # Group triggers by memory_id.
    triggers_by_mem: dict[int, list] = {}
    for t in triggers:
        mid = t["memory_id"]
        triggers_by_mem.setdefault(mid, []).append(t)

    # Stats.
    active = [m for m in memories if m["archived_ts"] is None]
    archived = [m for m in memories if m["archived_ts"] is not None]
    total_surfaces = sum(m["surface_count"] for m in active)
    total_useful = sum(m["useful_count"] for m in active)
    observer_procs = _count_observer_processes()
    observer_stats = _read_observer_stats()

    now_ts = int(time.time())

    def _ts(ts):
        if not ts:
            return "—"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    def _usefulness(m):
        return (m["useful_count"] + 1) / (m["surface_count"] + 2)

    def _bar(val, max_val):
        pct = min(100, int(val / max(max_val, 1) * 100))
        return f'<div class="bar"><div class="fill" style="width:{pct}%"></div></div>'

    max_surfaces = max((m["surface_count"] for m in active), default=1) or 1

    # Build memory rows.
    memory_rows = []
    for m in active:
        trigs = triggers_by_mem.get(m["id"], [])
        trig_tags = []
        for t in trigs:
            if t["kind"] == "tool_head":
                trig_tags.append(f'<span class="tag head">{t["tool_name"]}: {t["head_joined"]}</span>')
            elif t["kind"] == "path_glob":
                trig_tags.append(f'<span class="tag path">{t["path_pattern"]}</span>')
        trig_html = " ".join(trig_tags) or '<span class="tag none">no triggers</span>'

        u = _usefulness(m)
        u_class = "good" if u > 0.5 else "ok" if u > 0.2 else "low"
        scope_tag = f'<span class="tag scope-{m["scope"]}">{m["scope"]}</span>'

        memory_rows.append(f"""
        <tr class="memory-row" data-id="{m['id']}">
            <td class="id">{m['id']}</td>
            <td>
                <div class="name">{m['name']}</div>
                <div class="body">{m['body'][:200]}</div>
                <div class="triggers">{trig_html}</div>
            </td>
            <td><span class="tag {m['type']}">{m['type']}</span></td>
            <td>{scope_tag}</td>
            <td class="num">{m['surface_count']}{_bar(m['surface_count'], max_surfaces)}</td>
            <td class="num">{m['useful_count']}</td>
            <td class="num"><span class="usefulness {u_class}">{u:.0%}</span></td>
            <td class="ts">{_ts(m['last_surfaced_ts'])}</td>
            <td>{"📌" if m['pinned'] else ""}</td>
        </tr>""")

    # Archived rows.
    archived_rows = []
    for m in archived:
        archived_rows.append(f"""
        <tr class="archived-row">
            <td class="id">{m['id']}</td>
            <td><div class="name">{m['name']}</div></td>
            <td>{m['type']}</td>
            <td class="num">{m['surface_count']}</td>
            <td class="num">{m['useful_count']}</td>
            <td class="ts">{_ts(m['archived_ts'])}</td>
        </tr>""")

    # Surface log rows.
    surface_rows = []
    for s in surfaces:
        surface_rows.append(f"""
        <tr>
            <td class="ts">{_ts(s['surfaced_ts'])}</td>
            <td>{s['name']}</td>
            <td><span class="tag">{s['hook']}</span></td>
            <td class="mono">{s['session_id'][:12]}…</td>
        </tr>""")

    # Association rows.
    assoc_rows = []
    for a in associations:
        assoc_rows.append(f"""
        <tr>
            <td>{a['name_a']}</td>
            <td>{a['name_b']}</td>
            <td class="num">{a['strength']:.3f}</td>
            <td class="num">{a['co_fire_count']}</td>
            <td class="ts">{_ts(a['last_co_fire_ts'])}</td>
        </tr>""")

    # Consolidation rows.
    consol_rows = []
    for c in consolidations:
        consol_rows.append(f"""
        <tr>
            <td>{c['run_date']}</td>
            <td class="num">{c['sessions_scanned']}</td>
            <td class="num">{c['memories_archived']}</td>
            <td class="num">{c['memories_discovered']}</td>
        </tr>""")

    # Observer status indicator.
    obs_color = "#7ee787" if observer_procs > 0 else "#8b949e"
    obs_label = f"{observer_procs} running" if observer_procs > 0 else "idle"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ToolEngrams Dashboard</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 24px; }}
h1 {{ color: #58a6ff; margin-bottom: 8px; font-size: 24px; }}
h2 {{ color: #8b949e; font-size: 16px; margin: 24px 0 12px; border-bottom: 1px solid #21262d; padding-bottom: 8px; }}
.stats {{ display: flex; gap: 16px; margin: 16px 0; flex-wrap: wrap; }}
.stat {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px 20px; min-width: 120px; }}
.stat .val {{ font-size: 28px; font-weight: 600; color: #f0f6fc; }}
.stat .label {{ font-size: 12px; color: #8b949e; margin-top: 4px; }}
.stat .sub {{ font-size: 11px; color: #8b949e; margin-top: 2px; }}

/* Tabs */
.tabs {{ display: flex; gap: 0; margin: 24px 0 0; border-bottom: 1px solid #30363d; }}
.tab {{ padding: 10px 20px; cursor: pointer; color: #8b949e; font-size: 14px; font-weight: 500;
        border-bottom: 2px solid transparent; transition: all 0.15s; user-select: none; }}
.tab:hover {{ color: #c9d1d9; }}
.tab.active {{ color: #58a6ff; border-bottom-color: #58a6ff; }}
.tab-count {{ font-size: 11px; background: #21262d; border-radius: 10px; padding: 1px 7px; margin-left: 6px; }}
.tab-panel {{ display: none; padding-top: 16px; }}
.tab-panel.active {{ display: block; }}

table {{ width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; margin-bottom: 16px; }}
th {{ text-align: left; padding: 10px 12px; background: #21262d; color: #8b949e; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
td {{ padding: 10px 12px; border-top: 1px solid #21262d; vertical-align: top; font-size: 13px; }}
.id {{ color: #8b949e; font-size: 12px; }}
.name {{ font-weight: 600; color: #f0f6fc; }}
.body {{ color: #8b949e; font-size: 12px; margin: 4px 0; line-height: 1.4; }}
.triggers {{ margin-top: 4px; }}
.tag {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 500; }}
.tag.head {{ background: #1f3a5f; color: #58a6ff; }}
.tag.path {{ background: #2a1f3f; color: #bc8cff; }}
.tag.none {{ background: #3d1f1f; color: #f85149; }}
.tag.feedback {{ background: #2a3f1f; color: #7ee787; }}
.tag.reference {{ background: #1f3a5f; color: #58a6ff; }}
.tag.scope-global {{ background: #1a2733; color: #58a6ff; }}
.tag.scope-project {{ background: #2a1f3f; color: #bc8cff; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.ts {{ color: #8b949e; font-size: 12px; white-space: nowrap; }}
.mono {{ font-family: ui-monospace, monospace; font-size: 11px; color: #8b949e; }}
.bar {{ width: 60px; height: 4px; background: #21262d; border-radius: 2px; margin-top: 4px; }}
.fill {{ height: 100%; background: #58a6ff; border-radius: 2px; }}
.usefulness {{ font-weight: 600; }}
.usefulness.good {{ color: #7ee787; }}
.usefulness.ok {{ color: #d29922; }}
.usefulness.low {{ color: #f85149; }}
.archived-row td {{ opacity: 0.5; }}
.empty {{ color: #8b949e; padding: 20px; text-align: center; font-style: italic; }}
.obs-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }}
</style>
</head>
<body>

<h1>ToolEngrams</h1>
<p style="color:#8b949e; margin-bottom: 16px;">Generated {_ts(now_ts)}</p>

<div class="stats">
    <div class="stat"><div class="val">{len(active)}</div><div class="label">Active memories</div></div>
    <div class="stat"><div class="val">{len(archived)}</div><div class="label">Archived</div></div>
    <div class="stat"><div class="val">{total_surfaces}</div><div class="label">Total surfaces</div></div>
    <div class="stat"><div class="val">{total_useful}</div><div class="label">Total useful</div></div>
    <div class="stat"><div class="val">{len(associations)}</div><div class="label">Associations</div></div>
    <div class="stat">
        <div class="val"><span class="obs-dot" style="background:{obs_color}"></span>{obs_label}</div>
        <div class="label">Observer</div>
        <div class="sub">{observer_stats['today']} calls today / {observer_stats['total']} total</div>
    </div>
</div>

<div class="tabs">
    <div class="tab active" data-tab="memories">Memories<span class="tab-count">{len(active)}</span></div>
    <div class="tab" data-tab="surfaces">Surfaces<span class="tab-count">{len(surfaces)}</span></div>
    <div class="tab" data-tab="associations">Associations<span class="tab-count">{len(associations)}</span></div>
    <div class="tab" data-tab="consolidation">Consolidation<span class="tab-count">{len(consolidations)}</span></div>
    <div class="tab" data-tab="archived">Archived<span class="tab-count">{len(archived)}</span></div>
</div>

<div class="tab-panel active" id="memories">
<table>
<tr><th>#</th><th>Memory</th><th>Type</th><th>Scope</th><th>Surfaces</th><th>Useful</th><th>Score</th><th>Last Surfaced</th><th></th></tr>
{"".join(memory_rows) or '<tr><td colspan="9" class="empty">No active memories</td></tr>'}
</table>
</div>

<div class="tab-panel" id="surfaces">
<table>
<tr><th>Time</th><th>Memory</th><th>Hook</th><th>Session</th></tr>
{"".join(surface_rows) or '<tr><td colspan="4" class="empty">No surfaces recorded</td></tr>'}
</table>
</div>

<div class="tab-panel" id="associations">
<table>
<tr><th>Memory A</th><th>Memory B</th><th>Strength</th><th>Co-fires</th><th>Last</th></tr>
{"".join(assoc_rows) or '<tr><td colspan="5" class="empty">No associations yet</td></tr>'}
</table>
</div>

<div class="tab-panel" id="consolidation">
<table>
<tr><th>Date</th><th>Sessions</th><th>Archived</th><th>Discovered</th></tr>
{"".join(consol_rows) or '<tr><td colspan="4" class="empty">No consolidation runs</td></tr>'}
</table>
</div>

<div class="tab-panel" id="archived">
{"<table><tr><th>#</th><th>Name</th><th>Type</th><th>Surfaces</th><th>Useful</th><th>Archived</th></tr>" + "".join(archived_rows) + "</table>" if archived_rows else '<div class="empty">No archived memories</div>'}
</div>

<script>
document.querySelectorAll('.tab').forEach(tab => {{
    tab.addEventListener('click', () => {{
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(tab.dataset.tab).classList.add('active');
    }});
}});
</script>

</body>
</html>"""
