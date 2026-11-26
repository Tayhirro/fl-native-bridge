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


@pytest.mark.skipif(_candidate() is None, reason="isolated .pyd build not present")
def test_native_build_verification():
    """Isolated build: imports, table consistency, no-engine refusal, gate."""
    path = _candidate()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/verify-native-build.py"), str(path)],
        capture_output=True, text=True, timeout=30, cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "unregistered operation refused before handler" in result.stdout


def test_dispatch_table_matches_dependency_table():
    """Every dispatched operation must have a dependency declaration.

    Parses the C source directly so a newly added handler that forgets to
    declare its dependency fails CI even without compiling.
    """
    import re

    bridge = (ROOT / "fl_controller/native/bridge.c").read_text(encoding="utf-8")
    deps = (ROOT / "fl_controller/native/dependencies.c").read_text(encoding="utf-8")
    dispatched = set(re.findall(r'!strcmp\(operation,\s*"([^"]+)"\)', bridge))
    declared = set(re.findall(r'\{\s*"([a-z_]+\.[a-z_]+)"\s*,', deps))
    # The test-only operation is intentionally dispatched but undeclared.
    assert dispatched - declared == {"test.unregistered"}
    # Every declared operation is reachable (no stale dependency entries).
    assert declared - dispatched == set()
