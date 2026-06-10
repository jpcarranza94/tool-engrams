"""utils.prepend_engram_bin — the PATH seam for child claude sessions."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from toolengrams.utils import prepend_engram_bin

BIN_DIR = str(Path(sys.executable).parent)


def test_prepends_interpreter_bin_dir():
    env = {"PATH": "/usr/bin:/bin"}
    out = prepend_engram_bin(env)
    assert out["PATH"].split(os.pathsep)[0] == BIN_DIR
    assert out["PATH"].endswith("/usr/bin:/bin")


def test_idempotent_when_already_present():
    env = {"PATH": f"{BIN_DIR}{os.pathsep}/usr/bin"}
    out = prepend_engram_bin(env)
    assert out["PATH"].split(os.pathsep).count(BIN_DIR) == 1


def test_handles_missing_path():
    assert prepend_engram_bin({})["PATH"] == BIN_DIR
