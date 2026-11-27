"""Runs the JavaScript unit tests (``*.test.mjs``) of the web interface with Node."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_javascript_unit_tests() -> None:
    tests = sorted(str(path) for path in HERE.glob("*.test.mjs"))
    assert tests
    result = subprocess.run(
        ["node", "--test", *tests], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
