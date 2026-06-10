"""Fast unit tests for plugin/hook.sh — the shim's stamp logic without
building a real venv (a fake bootstrap.sh records spawns; a fake engram
records exec passthrough)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def shim_env(tmp_path):
    """A fake plugin ROOT (real hook.sh, fake bootstrap.sh) + empty DATA."""
    root = tmp_path / "root"
    (root / "plugin").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "plugin" / "hook.sh", root / "plugin" / "hook.sh")
    (root / "pyproject.toml").write_text("[project]\nname='x'\nversion='1'\n")
    fake_bootstrap = root / "plugin" / "bootstrap.sh"
    fake_bootstrap.write_text(
        "#!/bin/sh\necho \"spawned $1 $2\" >> \"$2/bootstrap.calls\"\n")
    fake_bootstrap.chmod(0o755)
    (root / "plugin" / "hook.sh").chmod(0o755)
    data = tmp_path / "data"
    return root, data


def _run(root, data, *args, stdin=""):
    return subprocess.run(
        [str(root / "plugin" / "hook.sh"), str(root), str(data), *args],
        input=stdin, capture_output=True, text=True, timeout=10)


def _stamp(root: Path) -> str:
    return (root / "pyproject.toml").read_text() + str(root)


def _wait_for(path: Path, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def test_dark_pretool_emits_empty_and_spawns_bootstrap(shim_env):
    root, data = shim_env
    proc = _run(root, data, "pretool", stdin="not json")
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
    _wait_for(data / "bootstrap.calls")  # detached spawn happened


def test_dark_session_start_reports_bootstrap_in_progress(shim_env):
    root, data = shim_env
    proc = _run(root, data, "session-start")
    out = json.loads(proc.stdout)
    assert "bootstrap in progress" in out["hookSpecificOutput"]["additionalContext"]


def test_dark_session_start_reports_failure_when_builds_keep_erroring(shim_env):
    root, data = shim_env
    data.mkdir()
    (data / "bootstrap.log").write_text("ERROR: venv creation failed\n")
    proc = _run(root, data, "session-start")
    out = json.loads(proc.stdout)
    assert "FAILING" in out["hookSpecificOutput"]["additionalContext"]


def test_current_stamp_execs_venv_engram(shim_env):
    root, data = shim_env
    bin_dir = data / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    fake_engram = bin_dir / "engram"
    fake_engram.write_text("#!/bin/sh\necho \"engram-ran $@ plugin=$ENGRAM_PLUGIN\"\n")
    fake_engram.chmod(0o755)
    (data / "install.stamp").write_text(_stamp(root))

    proc = _run(root, data, "pretool")
    assert "engram-ran pretool plugin=1" in proc.stdout
    assert not (data / "bootstrap.calls").exists()  # no spurious rebuild


def test_stale_stamp_fails_open_and_rebuilds_instead_of_exec(shim_env):
    root, data = shim_env
    bin_dir = data / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    fake_engram = bin_dir / "engram"
    fake_engram.write_text("#!/bin/sh\necho engram-ran\n")
    fake_engram.chmod(0o755)
    (data / "install.stamp").write_text("something stale")

    proc = _run(root, data, "stop")
    # Never exec into a venv a rebuild is about to clear.
    assert "engram-ran" not in proc.stdout
    assert json.loads(proc.stdout) == {}
    _wait_for(data / "bootstrap.calls")
