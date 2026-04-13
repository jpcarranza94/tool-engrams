"""Consolidation report generation."""

from __future__ import annotations

from .adjust import AdjustmentReport
from .collect import SessionFile
from .episodes import CorrectionEpisode, SurfacingEpisode


def format_report(
    *,
    target_date: str,
    sessions: list[SessionFile],
    surfacing_episodes: list[SurfacingEpisode],
    correction_episodes: list[CorrectionEpisode],
    adjustment: AdjustmentReport,
    is_dry_run: bool = False,
) -> str:
    lines: list[str] = []
    lines.append(f"{'[DRY RUN] ' if is_dry_run else ''}ToolEngrams consolidation report — {target_date}")
    lines.append("=" * 60)

    lines.append(f"\nSessions scanned: {len(sessions)}")
    total_size = sum(s.size_bytes for s in sessions)
    lines.append(f"Total transcript size: {total_size / 1024:.0f} KB")

    lines.append(f"\nSurfacing episodes extracted: {len(surfacing_episodes)}")
    if surfacing_episodes:
        succeeded = sum(1 for e in surfacing_episodes if e.tool_succeeded is True)
        failed = sum(1 for e in surfacing_episodes if e.tool_succeeded is False)
        unknown = len(surfacing_episodes) - succeeded - failed
        lines.append(f"  Tool succeeded: {succeeded} | Failed: {failed} | Unknown: {unknown}")

    lines.append(f"\nCorrection episodes detected: {len(correction_episodes)}")
    for ce in correction_episodes[:5]:
        preview = ce.user_message[:100].replace("\n", " ")
        lines.append(f"  [{ce.session_id[:8]}] {preview}")
    if len(correction_episodes) > 5:
        lines.append(f"  ... and {len(correction_episodes) - 5} more")

    lines.append(f"\nMechanical adjustments:")
    lines.append(f"  Archived (dead): {len(adjustment.archived_ids)}")
    for name in adjustment.archived_names:
        lines.append(f"    - {name}")
    lines.append(f"  Flagged stale: {len(adjustment.stale_ids)}")
    for name in adjustment.stale_names:
        lines.append(f"    - {name}")
    lines.append(f"  Session surfaces cleaned: {adjustment.surfaces_cleaned}")

    return "\n".join(lines)
