"""Automatic cleanup of cold watcher residue.

Three kinds of residue accumulate per watched session and nothing on the tick
path ever reclaims them:

  1. `watcher_state` rows for sessions whose transcript Claude Code has since
     deleted — dead cursors that the idle-sweep keeps re-statting forever.
  2. The stable sandbox cwds (`agent._work_dir`) under `agent._sandbox_root()`
     — user-only territory that no OS policy reaps, so this is their ONLY
     reaper. Plus legacy residue in the system temp dir (pre-stable-root
     sandboxes, crash-leaked mkdtemp dirs).
  3. The watcher sessions' own transcripts under `~/.claude/projects` (one
     internal project slug per sandbox cwd).

SessionStart calls `maybe_spawn_cleanup()`, which stats one marker file and —
at most once per `CLEANUP_INTERVAL_SEC` — spawns a detached `engram cleanup`
process that reaps everything older than the TTL. The hook itself never scans
the filesystem. An *active* session can't be reaped: staleness is judged by
the newest DIRECT child (every tick rewrites `delta.txt`; Claude Code keeps
appending to a live transcript), and the TTL is days while session activity
is minutes apart.

The marker is touched BEFORE spawning, so two concurrent SessionStarts may at
worst spawn two cleanups — every step is idempotent (DELETE by key, rmtree
ignore_errors), so that's wasted work, not corruption.

TTL via `$ENGRAM_CLEANUP_TTL_SEC` (default 7 days).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .. import db
from ..consolidation.collect import CLAUDE_PROJECTS_DIR, _is_internal_project
from ..utils import WATCHER_CHILD_ENV
from . import state
from .agent import _sandbox_root
from .log import _log

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON_BIN = sys.executable

# Residue younger than this is left alone. Days, not hours: the TTL must
# comfortably exceed any plausible gap inside a live session, so a live
# session's sandbox (its delta.txt + permission settings) is never reaped
# out from under a tick.
DEFAULT_CLEANUP_TTL_SEC = 7 * 86_400

# How often a cleanup may run, gated by the marker-file mtime. A constant, not
# an env knob — there is no reason to sweep more than daily.
CLEANUP_INTERVAL_SEC = 86_400

# Temp-dir basename prefixes we own and may reap. Mirrors
# hooks/_skip._INTERNAL_CWD_PREFIXES (not imported: hooks imports watcher, and
# this module is imported from watcher/__init__ — importing back would cycle).
_REAP_PREFIXES: tuple[str, ...] = (
    "engram-consolidate-",
    "engram-formation-",
    "engram-eval-",
    "engram-observe-",
    "engram-experiment-",
)


def _cleanup_ttl_sec() -> int:
    raw = os.environ.get("ENGRAM_CLEANUP_TTL_SEC", "")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CLEANUP_TTL_SEC
    return val if val > 0 else DEFAULT_CLEANUP_TTL_SEC


def _marker_path() -> Path:
    """The once-per-interval gate lives next to the DB (not in the temp dir,
    where the cleanup itself — or the OS — could reap it)."""
    return db.db_path().parent / "last-cleanup"


def maybe_spawn_cleanup() -> bool:
    """Hook-side gate: spawn a detached `engram cleanup` if the last one is
    older than `CLEANUP_INTERVAL_SEC`. Costs one stat on the common path.
    Returns True if a cleanup was spawned. Never raises."""
    try:
        marker = _marker_path()
        try:
            if time.time() - marker.stat().st_mtime < CLEANUP_INTERVAL_SEC:
                return False
        except OSError:
            pass  # missing marker → first run
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        env[WATCHER_CHILD_ENV] = "1"
        subprocess.Popen(
            [PYTHON_BIN, "-m", "toolengrams", "cleanup"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as e:
        _log(f"CLEANUP-SPAWN-ERROR error={e}")
        return False


def run_cleanup() -> int:
    """Reap all watcher residue older than the TTL. Runs in the detached
    `engram cleanup` process; each step is independent and best-effort."""
    ttl = _cleanup_ttl_sec()
    cutoff = time.time() - ttl
    is_ours = lambda name: name.startswith(_REAP_PREFIXES)
    rows = state.prune_dead_sessions(ttl)
    sandboxes = _reap_stale_dirs(_sandbox_root(), cutoff, is_ours)
    # Legacy residue in the system temp dir: pre-stable-root sandboxes and
    # crash-leaked mkdtemp dirs from the other internal agents.
    temp_dirs = _reap_stale_dirs(Path(tempfile.gettempdir()), cutoff, is_ours)
    slugs = _reap_stale_dirs(CLAUDE_PROJECTS_DIR, cutoff, _is_internal_project)
    _log(f"CLEANUP state_rows={rows} sandboxes={sandboxes} temp_dirs={temp_dirs} "
         f"watcher_transcripts={slugs} ttl_sec={ttl}")
    return 0


def _reap_stale_dirs(root: Path, cutoff: float, is_ours) -> int:
    """Remove direct subdirectories of `root` whose basename `is_ours` accepts
    and whose newest content is older than `cutoff`. Symlinks are never
    followed — only real directories we created qualify. Returns the count
    removed."""
    removed = 0
    try:
        children = list(root.iterdir())
    except OSError:
        return 0
    for child in children:
        try:
            if not is_ours(child.name):
                continue
            if child.is_symlink() or not child.is_dir():
                continue
            if _newest_mtime(child) >= cutoff:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def _newest_mtime(path: Path) -> float:
    """Newest mtime of `path` or any DIRECT child. Activity inside a dir
    (overwriting delta.txt, appending to a transcript) does not bump the dir's
    own mtime — only dirent churn does — so the dir stat alone would misread a
    live sandbox or transcript dir as cold."""
    newest = path.stat().st_mtime
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return newest
