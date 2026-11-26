"""Run the actual Google Docs JavaScript driver against a synthetic browser."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_web_docs_driver_regressions():
    fixture = Path(__file__).with_name("fixtures") / "web_docs_driver_regression.mjs"
    result = subprocess.run(
        ["node", "--test", str(fixture)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
