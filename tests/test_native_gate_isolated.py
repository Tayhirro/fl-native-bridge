"""Native gate failure matrix against an isolated .pyd build.

CI does not compile C, so this test skips unless a build is present at
FLMCP_NATIVE_PYD or the conventional temporary build path. When a build is
available it verifies: no-engine refusal, single-dependency isolation, and
that a refused operation never reaches its handler.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _candidate() -> Path | None:
    override = os.environ.get("FLMCP_NATIVE_PYD")
    candidates = [Path(override)] if override else []
    candidates.append(Path(os.environ.get("TEMP", "/tmp")) / "flmcp-build/_flmcp_native.pyd")
    for path in candidates:
        if path.is_file():
            return path
    return None


@pytest.mark.skipif(_candidate() is None, reason="isolated .pyd build not present")
def test_native_gate_failure_matrix():
    path = _candidate()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify-native-gate.py"), str(path)],
        capture_output=True, text=True, timeout=30, cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Gate verification passed" in result.stdout
    assert result.stdout.count("PASS ") >= 15
