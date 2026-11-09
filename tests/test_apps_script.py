"""Runs the Apps Script audit tests (tests/apps_script/*.test.js) with Node, if installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_audit_gs():
    tests = sorted(str(p) for p in (Path(__file__).parent / "apps_script").glob("*.test.js"))
    result = subprocess.run([NODE, "--test", *tests], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
