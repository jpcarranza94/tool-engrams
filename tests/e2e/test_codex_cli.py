from __future__ import annotations

import shutil
import subprocess

import pytest


@pytest.mark.e2e_codex
def test_codex_cli_version_smoke():
    codex_bin = shutil.which("codex")
    assert codex_bin is not None

    proc = subprocess.run([codex_bin, "--version"],
                          capture_output=True, text=True, timeout=10)

    assert proc.returncode == 0
    assert "codex" in (proc.stdout + proc.stderr).lower()
