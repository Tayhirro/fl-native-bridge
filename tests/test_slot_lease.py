"""Host-side slot lease tests for the FL controller.

A lease spans a compound FX write so a second MCP client cannot edit the same
slot between steps. The controller is the sole arbiter; all decisions are made
inside one OnIdle callback. A lease expires by TTL only, never stolen while
the original write outcome is still unknown.
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fl_controller"))


@pytest.fixture
def ctrl(monkeypatch):
    """Load the controller semantic module with a minimal FL host stub."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fl_controller_semantic_lease", ROOT / "fl_controller" / "semantic_native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._slot_leases.clear()
    return module


def test_claim_and_release_same_owner(ctrl):
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    # Re-entrant claim by the same owner/token is fine.
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    ctrl.release_slot(3, 5, "ctx-A", "token-1")
    assert not ctrl._slot_leases


def test_second_owner_cannot_claim_same_slot(ctrl):
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    with pytest.raises(RuntimeError, match="reserved"):
        ctrl.claim_slot(3, 5, "ctx-B", "token-2")
    # The original owner still holds it.
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    assert ctrl._slot_leases[(3, 5)][0] == "ctx-A"


def test_same_owner_different_token_cannot_claim(ctrl):
    """A new request from the same process must not steal a live lease."""
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    with pytest.raises(RuntimeError, match="reserved"):
        ctrl.claim_slot(3, 5, "ctx-A", "token-2")


def test_different_slots_are_independent(ctrl):
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    ctrl.claim_slot(3, 7, "ctx-B", "token-2")  # no conflict
    assert len(ctrl._slot_leases) == 2


def test_lease_expires_by_ttl_only(ctrl, monkeypatch):
    now = {"t": 100.0}
    monkeypatch.setattr(ctrl.time, "monotonic", lambda: now["t"])
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    now["t"] += ctrl._LEASE_SECONDS - 1
    with pytest.raises(RuntimeError, match="reserved"):
        ctrl.claim_slot(3, 5, "ctx-B", "token-2")
    now["t"] += 2  # past TTL
    ctrl.claim_slot(3, 5, "ctx-B", "token-2")
    assert ctrl._slot_leases[(3, 5)][0] == "ctx-B"


def test_release_only_by_owner(ctrl):
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    ctrl.release_slot(3, 5, "ctx-B", "token-2")
    assert (3, 5) in ctrl._slot_leases
    ctrl.release_slot(3, 5, "ctx-A", "token-1")
    assert (3, 5) not in ctrl._slot_leases


def test_invalid_slot_rejected(ctrl):
    with pytest.raises(ValueError, match="Invalid slot"):
        ctrl.claim_slot(3, 99, "ctx-A", "token-1")


def test_verify_requires_live_lease(ctrl, monkeypatch):
    monkeypatch.setattr(ctrl, "effect_context", lambda: "ctx-A")
    monkeypatch.setattr(ctrl.transport, "isPlaying", lambda: False)
    monkeypatch.setattr(ctrl.transport, "isRecording", lambda: False)
    general = types.SimpleNamespace(safeToEdit=lambda: True)
    monkeypatch.setitem(sys.modules, "general", general)
    with pytest.raises(RuntimeError, match="lease is missing"):
        ctrl.verify_slot_context(3, 5, "ctx-A", "token-1")
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    ctrl.verify_slot_context(3, 5, "ctx-A", "token-1")  # now passes


def test_verify_stops_on_playback(ctrl, monkeypatch):
    monkeypatch.setattr(ctrl, "effect_context", lambda: "ctx-A")
    monkeypatch.setattr(ctrl.transport, "isPlaying", lambda: True)
    monkeypatch.setattr(ctrl.transport, "isRecording", lambda: False)
    monkeypatch.setitem(sys.modules, "general", types.SimpleNamespace(safeToEdit=lambda: True))
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    with pytest.raises(RuntimeError, match="playback or recording"):
        ctrl.verify_slot_context(3, 5, "ctx-A", "token-1")


def test_verify_stops_on_project_change(ctrl, monkeypatch):
    monkeypatch.setattr(ctrl, "effect_context", lambda: "ctx-B")
    monkeypatch.setattr(ctrl.transport, "isPlaying", lambda: False)
    monkeypatch.setattr(ctrl.transport, "isRecording", lambda: False)
    monkeypatch.setitem(sys.modules, "general", types.SimpleNamespace(safeToEdit=lambda: True))
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    with pytest.raises(RuntimeError, match="project changed"):
        ctrl.verify_slot_context(3, 5, "ctx-A", "token-1")
