"""launchd plist generation for nightly consolidation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PLIST_NAME = "com.toolengrams.consolidate"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{PLIST_NAME}.plist"


def generate_plist(use_agent: bool = False) -> str:
    engram_bin = shutil.which("engram")
    if not engram_bin:
        raise RuntimeError("engram not found on PATH — install with: uv pip install --system -e .")

    args = ["--yesterday", "--json"]
    if use_agent:
        args.append("--agent")

    args_xml = "\n".join(f"        <string>{a}</string>" for a in args)

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
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{Path.home()}/.claude/tool-engrams/consolidate.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/.claude/tool-engrams/consolidate.err</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


def install_schedule(use_agent: bool = False) -> str:
    """Write plist and load into launchd. Returns the plist path."""
    PLIST_DIR.mkdir(parents=True, exist_ok=True)

    # Unload existing if present.
    if PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(PLIST_PATH)],
            capture_output=True,
        )

    plist_content = generate_plist(use_agent=use_agent)
    PLIST_PATH.write_text(plist_content)

    # Ensure log directory exists.
    log_dir = Path.home() / ".claude" / "tool-engrams"
    log_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        check=True,
        capture_output=True,
    )

    return str(PLIST_PATH)


def uninstall_schedule() -> bool:
    """Unload and remove the plist. Returns True if it existed."""
    if not PLIST_PATH.exists():
        return False
    subprocess.run(
        ["launchctl", "unload", str(PLIST_PATH)],
        capture_output=True,
    )
    PLIST_PATH.unlink()
    return True
