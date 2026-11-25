"""The legacy MIDI entry must not execute idle-only native operations.

FL Studio invokes controller callbacks on its main thread. The file-queue
consumer runs in OnIdle; legacy commands arrive through OnMidiMsg. Without the
_idle guard, a MIDI-triggered semantic operation could re-enter the queue
consumer and touch FL memory from a nested callback.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_controller(name, file, monkeypatch):
    path = ROOT / "fl_controller" / file
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def device(monkeypatch):
    modules = {
        "channels": {}, "midi": {}, "mixer": {}, "plugins": {}, "device": {},
        "transport": {}, "general": {}, "patterns": {}, "playlist": {},
        "arrangement": {}, "ui": {},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        vars(module).update(values)
        monkeypatch.setitem(sys.modules, name, module)
    # device_FLStudioMCP reloads these modules on import; they must be
    # importable by name from sys.path for importlib.reload to work.
    monkeypatch.syspath_prepend(str(ROOT / "fl_controller"))
    for name in ("bridge_extension", "native_rpc"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    # No semantic layer: the tests supply their own stub after import.
    monkeypatch.delitem(sys.modules, "semantic_native", raising=False)

    path = ROOT / "fl_controller/device_FLStudioMCP.py"
    spec = importlib.util.spec_from_file_location("device_idle_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_midi_entry_refuses_idle_only_action(device, monkeypatch):
    dispatched = []

    class Semantic:
        ACTIONS = frozenset({"native.movePlaylistClip"})

        @staticmethod
        def dispatch(action, params):
            dispatched.append((action, params))
            return {"success": True}

    monkeypatch.setattr(device, "semantic_native", Semantic)
    monkeypatch.setattr(device, "_native_idle_active", False)
    result = device.dispatch_command("native.movePlaylistClip", {})
    assert not result["success"]
    assert "idle queue" in result["error"]
    assert dispatched == []


def test_idle_entry_allows_idle_only_action(device, monkeypatch):
    dispatched = []

    class Semantic:
        ACTIONS = frozenset({"native.movePlaylistClip"})

        @staticmethod
        def dispatch(action, params):
            dispatched.append((action, params))
            return {"success": True, "ok": 1}

    monkeypatch.setattr(device, "semantic_native", Semantic)
    monkeypatch.setattr(device, "_native_idle_active", True)
    result = device.dispatch_command("native.movePlaylistClip", {"x": 1})
    assert result["success"] and result["ok"] == 1
    assert len(dispatched) == 1


def test_on_idle_always_clears_active_flag(device, monkeypatch):
    def fail_poll(_dispatch):
        raise RuntimeError("poll failed")

    monkeypatch.setattr(device.native_rpc, "poll", fail_poll)
    with pytest.raises(RuntimeError):
        device.OnIdle()
    assert device._native_idle_active is False
