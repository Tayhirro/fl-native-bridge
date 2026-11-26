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


def test_load_effect_rechecks_lease_after_write(ctrl, monkeypatch, tmp_path):
    """The controller's insert path must verify the lease before and after."""

    preset = tmp_path / "delay.fst"
    preset.write_bytes(b"FLhd" + b"\0" * 18 + b"FLdt" + b"\0" * 4)
    calls = []

    class Loader:
        def initialize(self):
            return "engine-path"

        def execute(self, op, args):
            calls.append((op, args))
            return {}

    monkeypatch.setattr(ctrl, "effect_context", lambda: "ctx-A")
    monkeypatch.setattr(ctrl, "native_effect_loader", lambda: Loader())
    monkeypatch.setattr(ctrl, "read_effect_slot", lambda p: {
        "occupied": False, "track": 3, "slot": 5})
    monkeypatch.setattr(ctrl.transport, "isPlaying", lambda: False)
    monkeypatch.setattr(ctrl.transport, "isRecording", lambda: False)
    monkeypatch.setitem(sys.modules, "general", types.SimpleNamespace(safeToEdit=lambda: True))

    params = {"name": "Fruity Delay 3", "context": "ctx-A",
              "preset_path": str(preset), "dry_run": False,
              "lease_token": "token-1", "track": 3, "slot": 5}

    # Without a claim, the insert must be refused before the native call.
    with pytest.raises(RuntimeError, match="lease is missing"):
        ctrl.load_effect_at_slot(params)
    assert not calls

    # With a claim, the insert runs and is verified.
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")
    result = ctrl.load_effect_at_slot(params)
    assert result["success"] is True
    assert result["submitted"] is True
    assert calls[0][0] == "mixer.insert_empty_slot"


def test_load_effect_preserves_submitted_when_post_check_fails(ctrl, monkeypatch, tmp_path):
    """A lease lost during the write must not erase submitted=True."""
    preset = tmp_path / "delay.fst"
    preset.write_bytes(b"FLhd" + b"\0" * 18 + b"FLdt" + b"\0" * 4)
    executed = []

    class Loader:
        def initialize(self):
            return "engine-path"

        def execute(self, op, args):
            executed.append(op)
            # Simulate another caller stealing the lease mid-write.
            ctrl._slot_leases.pop((3, 5), None)
            return {}

    monkeypatch.setattr(ctrl, "effect_context", lambda: "ctx-A")
    monkeypatch.setattr(ctrl, "native_effect_loader", lambda: Loader())
    monkeypatch.setattr(ctrl, "read_effect_slot", lambda p: {
        "occupied": False, "track": 3, "slot": 5})
    monkeypatch.setattr(ctrl.transport, "isPlaying", lambda: False)
    monkeypatch.setattr(ctrl.transport, "isRecording", lambda: False)
    monkeypatch.setitem(sys.modules, "general", types.SimpleNamespace(safeToEdit=lambda: True))
    ctrl.claim_slot(3, 5, "ctx-A", "token-1")

    result = ctrl.load_effect_at_slot({
        "name": "Fruity Delay 3", "context": "ctx-A",
        "preset_path": str(preset), "dry_run": False,
        "lease_token": "token-1", "track": 3, "slot": 5,
    })
    assert len(executed) == 1  # the write really happened
    assert result["success"] is False
    assert result["submitted"] is True
    assert result["verified"] is False
