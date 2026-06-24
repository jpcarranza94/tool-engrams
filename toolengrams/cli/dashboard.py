"""engram dashboard — open a local HTML dashboard in the browser."""

from __future__ import annotations

import html
import sqlite3
import tempfile
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from .. import db, memory_store
from ..consolidation import runs as consolidation_runs
from ..retrieval import session_state
from ..watcher.log import log_path

# The watcher is no longer a long-lived process, so "active" = a tracked session
# that ticked within this window, not a live PID.
ACTIVE_WATCHER_WINDOW_SEC = 900


def main(argv: list[str] | None = None) -> int:
    with db.session() as conn:
        html = _build_html(conn)
        path = Path(tempfile.gettempdir()) / "engram-dashboard.html"
        path.write_text(html)
        webbrowser.open(f"file://{path}")
        print(f"Dashboard opened: {path}")
        return 0


def _count_active_watchers(conn: sqlite3.Connection) -> int:
    """How many tracked sessions ticked recently — the dashboard's
    "is anyone being watched right now?" signal.

    The watcher is no longer a long-lived process (no `watcher_pid` to probe),
    so liveness is recent tick activity: a `last_tick_ts` within
    ACTIVE_WATCHER_WINDOW_SEC.
    """
    try:
        cutoff = int(time.time()) - ACTIVE_WATCHER_WINDOW_SEC
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM watcher_state WHERE last_tick_ts >= ?",
            (cutoff,),
        ).fetchone()
        return row["c"] if row else 0
    except Exception:
        return 0


def _read_watcher_stats() -> dict:
    """Parse watcher.log for recent activity stats."""
    stats = {"total": 0, "today": 0, "last_entry": "---"}
    try:
        path = log_path()
        if not path.exists():
            return stats
        lines = path.read_text().splitlines()
        stats["total"] = len(lines)
        today = time.strftime("%Y-%m-%d")
        stats["today"] = sum(1 for l in lines if l.startswith(today))
        if lines:
            stats["last_entry"] = lines[-1][:19]  # timestamp portion
    except Exception:
        pass
    return stats


def _render_report(text: str | None) -> str:
    """Render a consolidation report (markdown-ish prose) to safe HTML.

    No markdown dependency on the dashboard path — escape everything, then
    lightly style headings / bullets / fenced code line by line. Unknown lines
    pass through as plain paragraphs, so a malformed report still degrades to
    readable text. Heading prefixes ("# ", "## ", "### ") contain no characters
    html.escape rewrites, so slicing the escaped string by the prefix length is
    safe.
    """
    if not text or not text.strip():
        return '<div class="report-empty">No report recorded for this run.</div>'
    out: list[str] = []
    in_code = False
    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        esc = html.escape(raw)
        if in_code:
            out.append(f'<div class="r-code">{esc or "&nbsp;"}</div>')
        elif raw.startswith("### "):
            out.append(f'<div class="r-h3">{esc[4:]}</div>')
        elif raw.startswith("## "):
            out.append(f'<div class="r-h2">{esc[3:]}</div>')
        elif raw.startswith("# "):
            out.append(f'<div class="r-h1">{esc[2:]}</div>')
        elif raw.lstrip().startswith(("- ", "* ")):
            out.append(f'<div class="r-li">{esc}</div>')
        elif not raw.strip():
            out.append('<div class="r-gap"></div>')
        else:
            out.append(f'<div class="r-p">{esc}</div>')
    return '<div class="report">' + "".join(out) + "</div>"


_SEVERITY_RANK = {"info": 0, "warn": 1, "critical": 2}


def _group_recommendations(rows) -> list[dict]:
    """Collapse cross-run recommendation rows into one entry per title.

    Rows arrive newest-run-first (see runs.recommendations_across_runs). Items
    are deduped by casefolded title so a recurring advisory shows once with every
    date it was raised. Per group: the **most recent** occurrence supplies the
    display title and status (the current state of the issue); severity is the
    **max** seen (a single critical run keeps the group critical); detail/issue_url
    take the most recent non-empty value. Output is sorted severity-desc then
    newest-date-desc so the loudest, freshest items lead.

    Pure over a list of mappings (sqlite3.Row or dict) — no DB access — so the
    dedup/grouping logic is unit-testable without rendering.
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        title = r["title"]
        key = title.strip().casefold()
        g = groups.get(key)
        if g is None:
            # First sighting == most recent run (rows are newest-first): seed the
            # "current state" fields here and never overwrite them.
            g = {"title": title, "severity": r["severity"], "status": r["status"],
                 "detail": r["detail"], "issue_url": r["issue_url"], "dates": []}
            groups[key] = g
            order.append(key)
        if r["run_date"] not in g["dates"]:
            g["dates"].append(r["run_date"])
        if _SEVERITY_RANK.get(r["severity"], 0) > _SEVERITY_RANK.get(g["severity"], 0):
            g["severity"] = r["severity"]
        if not g["detail"] and r["detail"]:
            g["detail"] = r["detail"]
        if not g["issue_url"] and r["issue_url"]:
            g["issue_url"] = r["issue_url"]
    grouped = [groups[k] for k in order]
    grouped.sort(key=lambda g: (_SEVERITY_RANK.get(g["severity"], 0), g["dates"][0]),
                 reverse=True)
    return grouped


def _build_html(conn: sqlite3.Connection) -> str:
    memories = memory_store.list_memories(conn, include_archived=True, order="dashboard")
    triggers = memory_store.all_triggers(conn)

    surfaces = session_state.recent_surfaces_with_memory(conn, limit=50)
    consolidations = consolidation_runs.recent_runs(conn, limit=10)
    recommendations = _group_recommendations(
        consolidation_runs.recommendations_across_runs(conn, run_limit=10))

    # Group triggers by memory_id.
    triggers_by_mem: dict[int, list] = {}
    for t in triggers:
        triggers_by_mem.setdefault(t.memory_id, []).append(t)

    # Stats.
    active = [m for m in memories if m.archived_ts is None]
    archived = [m for m in memories if m.archived_ts is not None]
    total_surfaces = sum(m.surface_count for m in active)
    total_useful = sum(m.useful_count for m in active)
    watcher_alive = _count_active_watchers(conn)
    watcher_stats = _read_watcher_stats()

    now_ts = int(time.time())

    def _ts(ts):
        if not ts:
            return "—"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    def _usefulness(m):
        # q: noise-aware quality. surface_count is telemetry now.
        return (m.useful_count + 1) / (m.useful_count + m.noise_count + 2)

    def _bar(val, max_val):
        pct = min(100, int(val / max(max_val, 1) * 100))
        return f'<div class="bar"><div class="fill" style="width:{pct}%"></div></div>'

    max_surfaces = max((m.surface_count for m in active), default=1) or 1

    # Build memory rows.
    memory_rows = []
    for m in active:
        trigs = triggers_by_mem.get(m.id, [])
        trig_tags = []
        for t in trigs:
            if t.kind == "token_subseq":
                label = " ".join(t.tokens) if t.tokens else (t.first_token or "?")
                trig_tags.append(f'<span class="tag head">{label}</span>')
            elif t.kind == "path_glob":
                trig_tags.append(f'<span class="tag path">{t.path_pattern}</span>')
        trig_html = " ".join(trig_tags) or '<span class="tag none">no triggers</span>'

        u = _usefulness(m)
        u_class = "good" if u > 0.5 else "ok" if u > 0.2 else "low"
        scope_tag = f'<span class="tag scope-{m.scope}">{m.scope}</span>'

        memory_rows.append(f"""
        <tr class="memory-row" data-id="{m.id}">
            <td class="id">{m.id}</td>
            <td>
                <div class="name">{m.name}</div>
                <div class="body">{m.body[:200]}</div>
                <div class="triggers">{trig_html}</div>
            </td>
            <td><span class="tag {m.kind}">{m.kind}</span></td>
            <td>{scope_tag}</td>
            <td class="num">{m.surface_count}{_bar(m.surface_count, max_surfaces)}</td>
            <td class="num">{m.useful_count}</td>
            <td class="num"><span class="usefulness {u_class}">{u:.0%}</span></td>
            <td class="ts">{_ts(m.last_surfaced_ts)}</td>
            <td>{"📌" if m.pinned else ""}</td>
        </tr>""")

    # Archived rows.
    archived_rows = []
    for m in archived:
        archived_rows.append(f"""
        <tr class="archived-row">
            <td class="id">{m.id}</td>
            <td><div class="name">{m.name}</div></td>
            <td>{m.kind}</td>
            <td class="num">{m.surface_count}</td>
            <td class="num">{m.useful_count}</td>
            <td class="ts">{_ts(m.archived_ts)}</td>
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

    # Consolidation rows — each summary row expands to show the run's full
    # report (the agent's prose recommendations), which is otherwise invisible.
    consol_rows = []
    for c in consolidations:
        qs = c['quality_score']
        qs_display = f"{qs:.0%}" if qs is not None else "—"
        qs_class = "good" if qs and qs >= 0.6 else "ok" if qs and qs >= 0.3 else "low" if qs is not None else ""
        report_keys = c.keys() if hasattr(c, "keys") else []
        report = c["report"] if "report" in report_keys else None
        consol_rows.append(f"""
        <tr class="consol-row">
            <td><span class="caret">▸</span>{c['run_date']}</td>
            <td class="num">{c['sessions_scanned']}</td>
            <td class="num">{c['episodes_evaluated'] or 0}</td>
            <td class="num">{c['surfaces_helpful'] or 0}</td>
            <td class="num">{c['surfaces_noise'] or 0}</td>
            <td class="num"><span class="usefulness {qs_class}">{qs_display}</span></td>
            <td class="num">{c['memories_discovered'] or 0}</td>
            <td class="num">{c['memories_archived'] or 0}</td>
        </tr>
        <tr class="consol-detail"><td colspan="8">{_render_report(report)}</td></tr>""")

    # Recommendation rows — one per recurring advisory, deduped across runs.
    rec_rows = []
    for g in recommendations:
        title_esc = html.escape(g["title"])
        if g["issue_url"]:
            title_html = (f'<a class="rec-link" href="{html.escape(g["issue_url"])}" '
                          f'target="_blank" rel="noopener">{title_esc}</a>')
        else:
            title_html = title_esc
        detail_html = (f'<div class="rec-detail">{html.escape(g["detail"])}</div>'
                       if g["detail"] else "")
        dates_html = ", ".join(html.escape(d) for d in g["dates"])
        rec_rows.append(f"""
        <tr>
            <td><div class="rec-title">{title_html}</div>{detail_html}</td>
            <td><span class="tag sev-{g['severity']}">{g['severity']}</span></td>
            <td><span class="tag st-{g['status']}">{g['status']}</span></td>
            <td class="rec-dates">{dates_html}</td>
        </tr>""")

    # Watcher status indicator.
    obs_color = "#7ee787" if watcher_alive > 0 else "#8b949e"
    obs_label = f"{watcher_alive} active" if watcher_alive > 0 else "idle"

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
.tag.block {{ background: #3d1f1f; color: #f85149; }}
.tag.hint {{ background: #1f3a5f; color: #58a6ff; }}
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

/* Consolidation: expandable report rows */
.consol-row {{ cursor: pointer; }}
.consol-row:hover td {{ background: #1c2230; }}
.caret {{ display: inline-block; color: #8b949e; margin-right: 6px; transition: transform 0.15s; }}
.consol-row.open .caret {{ transform: rotate(90deg); }}
.consol-detail {{ display: none; }}
.consol-detail.open {{ display: table-row; }}
.consol-detail td {{ background: #0d1117; padding: 0; }}
.report {{ font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px; line-height: 1.5;
           border: 1px solid #21262d; border-radius: 6px; margin: 8px; padding: 14px 18px;
           max-height: 520px; overflow: auto; }}
.report-empty {{ color: #8b949e; font-style: italic; padding: 14px 18px; }}
.r-h1 {{ color: #58a6ff; font-weight: 700; font-size: 14px; margin: 8px 0 4px; }}
.r-h2 {{ color: #bc8cff; font-weight: 700; margin: 12px 0 2px; }}
.r-h3 {{ color: #79c0ff; font-weight: 600; margin: 8px 0 2px; }}
.r-li {{ color: #c9d1d9; padding-left: 10px; }}
.r-p {{ color: #c9d1d9; }}
.r-code {{ color: #7ee787; white-space: pre-wrap; }}
.r-gap {{ height: 8px; }}

/* Recommendations (cross-run, deduped by title) */
.rec-title {{ font-weight: 600; color: #f0f6fc; }}
.rec-link {{ color: #58a6ff; text-decoration: none; }}
.rec-link:hover {{ text-decoration: underline; }}
.rec-detail {{ color: #8b949e; font-size: 12px; margin-top: 4px; line-height: 1.4; }}
.rec-dates {{ color: #8b949e; font-size: 12px; white-space: nowrap; }}
.tag.sev-info {{ background: #1a2733; color: #58a6ff; }}
.tag.sev-warn {{ background: #3a2f1a; color: #d29922; }}
.tag.sev-critical {{ background: #3d1f1f; color: #f85149; }}
.tag.st-open {{ background: #21262d; color: #8b949e; }}
.tag.st-done {{ background: #1a3326; color: #7ee787; }}
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
    <div class="stat">
        <div class="val"><span class="obs-dot" style="background:{obs_color}"></span>{obs_label}</div>
        <div class="label">Watcher</div>
        <div class="sub">{watcher_stats['today']} events today / {watcher_stats['total']} total</div>
    </div>
</div>

<div class="tabs">
    <div class="tab active" data-tab="memories">Memories<span class="tab-count">{len(active)}</span></div>
    <div class="tab" data-tab="surfaces">Surfaces<span class="tab-count">{len(surfaces)}</span></div>
    <div class="tab" data-tab="consolidation">Consolidation<span class="tab-count">{len(consolidations)}</span></div>
    <div class="tab" data-tab="recommendations">Recommendations<span class="tab-count">{len(recommendations)}</span></div>
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

<div class="tab-panel" id="consolidation">
<table>
<tr><th>Date</th><th>Sessions</th><th>Evaluated</th><th>Helpful</th><th>Noise</th><th>Quality</th><th>Created</th><th>Pruned</th></tr>
{"".join(consol_rows) or '<tr><td colspan="8" class="empty">No consolidation runs</td></tr>'}
</table>
</div>

<div class="tab-panel" id="recommendations">
<table>
<tr><th>Recommendation</th><th>Severity</th><th>Status</th><th>Raised on</th></tr>
{"".join(rec_rows) or '<tr><td colspan="4" class="empty">No recommendations recorded</td></tr>'}
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

// Consolidation rows: click to toggle the report detail row beneath.
document.querySelectorAll('.consol-row').forEach(row => {{
    row.addEventListener('click', () => {{
        const detail = row.nextElementSibling;
        row.classList.toggle('open');
        if (detail && detail.classList.contains('consol-detail')) {{
            detail.classList.toggle('open');
        }}
    }});
}});
</script>

</body>
</html>"""
