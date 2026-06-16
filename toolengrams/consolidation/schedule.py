"""Platform-aware scheduling for nightly consolidation.

macOS: launchd plist in ~/Library/LaunchAgents/
Linux: cron job via crontab
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

from ..paths import engram_home

PLIST_NAME = "com.toolengrams.consolidate"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{PLIST_NAME}.plist"
CRON_MARKER = "# toolengrams-consolidate"

# Resolved at import time of the installing process and baked into the
# launchd plist / cron line — the scheduled job runs with a minimal env where
# $ENGRAM_HOME may be absent, so the resolved path (not the env var) is what
# must persist.
LOG_DIR = engram_home()


def _get_platform() -> str:
    return platform.system()  # "Darwin" or "Linux"


def _find_engram() -> str:
    engram_bin = shutil.which("engram")
    if not engram_bin:
        raise RuntimeError("engram not found on PATH — install with: uv pip install --system -e .")
    return engram_bin


# ---------- public API ----------


def install_schedule() -> str:
    """Install the nightly consolidation schedule. Returns path/description."""
    plat = _get_platform()
    if plat == "Darwin":
        return _install_launchd()
    elif plat == "Linux":
        return _install_cron()
    else:
        raise RuntimeError(f"Unsupported platform: {plat}. Supported: macOS (launchd), Linux (cron).")


def uninstall_schedule() -> bool:
    """Remove the nightly schedule. Returns True if it existed."""
    plat = _get_platform()
    if plat == "Darwin":
        return _uninstall_launchd()
    elif plat == "Linux":
        return _uninstall_cron()
    return False


def is_installed() -> bool:
    """Check if a schedule is currently installed."""
    plat = _get_platform()
    if plat == "Darwin":
        return PLIST_PATH.exists()
    elif plat == "Linux":
        return _cron_entry_exists()
    return False


# ---------- macOS: launchd ----------


def _resolve_plist_path() -> str:
    """Build a PATH the launchd job can use.

    launchd gives jobs a minimal `/usr/bin:/bin:/usr/sbin:/sbin`, which
    means `shutil.which("claude")` inside the spawned engram process
    returns None and the consolidation agent bails with "claude CLI not
    found on PATH". Resolve `claude` and `engram` at install time and
    include both their parent dirs plus the common user-bin paths.
    """
    parts: list[str] = []

    def _add_parent(binary: str) -> None:
        p = shutil.which(binary)
        if p:
            parent = str(Path(p).parent)
            if parent not in parts:
                parts.append(parent)

    _add_parent("engram")
    _add_parent("claude")
    # Common toolchain dirs that often carry jq, git, sqlite3, etc.
    for fixed in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        if fixed not in parts:
            parts.append(fixed)

    return ":".join(parts)


def _generate_plist() -> str:
    engram_bin = _find_engram()
    args = ["--yesterday", "--json"]
    args_xml = "\n".join(f"        <string>{a}</string>" for a in args)
    path_env = _resolve_plist_path()

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{engram_bin}</string>
        <string>consolidate</string>
{args_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_env}</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{LOG_DIR}/consolidate.log</string>
    <key>StandardErrorPath</key>
    <string>{LOG_DIR}/consolidate.err</string>
    <!-- Run on login/load too, not only at 8 AM. The job is a catch-up sweep
         guarded by was_run() idempotency, so firing it on every boot is safe
         (already-done days are skipped) and drains any backlog promptly when
         the laptop was off at 8 AM. -->
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""


def _install_launchd() -> str:
    PLIST_DIR.mkdir(parents=True, exist_ok=True)

    if PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(PLIST_PATH)],
            capture_output=True,
        )

    PLIST_PATH.write_text(_generate_plist())
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        check=True,
        capture_output=True,
    )

    return str(PLIST_PATH)


def _uninstall_launchd() -> bool:
    if not PLIST_PATH.exists():
        return False
    subprocess.run(
        ["launchctl", "unload", str(PLIST_PATH)],
        capture_output=True,
    )
    PLIST_PATH.unlink()
    return True


# ---------- Linux: cron ----------


def _build_cron_line() -> str:
    engram_bin = _find_engram()
    log_path = LOG_DIR / "consolidate.log"
    return f"0 8 * * * {engram_bin} consolidate --yesterday --json >> {log_path} 2>&1 {CRON_MARKER}"


def _get_current_crontab() -> str:
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _cron_entry_exists() -> bool:
    return CRON_MARKER in _get_current_crontab()


def _install_cron() -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    current = _get_current_crontab()

    # Remove existing entry if present.
    lines = [l for l in current.splitlines() if CRON_MARKER not in l]
    lines.append(_build_cron_line())
    new_crontab = "\n".join(lines) + "\n"

    proc = subprocess.run(
        ["crontab", "-"],
        input=new_crontab, text=True,
        capture_output=True, timeout=5,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to install cron job: {proc.stderr}")

    return f"cron: daily at 08:00 ({CRON_MARKER})"


def _uninstall_cron() -> bool:
    current = _get_current_crontab()
    if CRON_MARKER not in current:
        return False

    lines = [l for l in current.splitlines() if CRON_MARKER not in l]
    new_crontab = "\n".join(lines) + "\n" if lines else ""

    subprocess.run(
        ["crontab", "-"],
        input=new_crontab, text=True,
        capture_output=True, timeout=5,
    )
    return True
