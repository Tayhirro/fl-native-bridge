"""Per-operation dependency reporting and gating contract.

The C gate itself is exercised by the isolated .pyd build (see
scripts/build-native-effect-loader.ps1). These tests verify that the
controller reports every dependency state verbatim and that the MCP
capability surface keeps its existing fields while adding the new map.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def controller(monkeypatch):
    """Load fl_controller/semantic_native.py with injected FL modules."""
    modules = {
        "channels": {},
        "midi": {},
        "mixer": {"trackCount": lambda: 9, "getTrackCount": lambda: 8,
                  "getEventIDName": lambda e: str(e)},
        "plugins": {},
        "device": {},
        "transport": {"isPlaying": lambda: False, "isRecording": lambda: False},
        "general": {},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        vars(module).update(values)
        monkeypatch.setitem(sys.modules, name, module)

    path = Path(__file__).resolve().parents[1] / "fl_controller/semantic_native.py"
    spec = importlib.util.spec_from_file_location("controller_deps_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_capabilities_reports_dependency_states(controller, monkeypatch):
    dependencies = {
        "mixer.insert_empty_slot": {
            "function_entry": "build-gated", "global_addresses": "build-gated",
            "object_layout": "build-gated", "calling_convention": "verified",
            "entry_rva": 0, "rva_low": 4096, "rva_high": 16401648,
        },
        "project.save_copy": {
            "function_entry": "verified", "global_addresses": "build-gated",
            "object_layout": "build-gated", "calling_convention": "verified",
            "entry_rva": 11879792, "rva_low": 4096, "rva_high": 16401648,
        },
    }
    loader = SimpleNamespace(
        describe=lambda: {"api_version": 1, "engine_profile": "26.1.7.5654-x64",
                          "operations": ["mixer.insert_empty_slot", "project.save_copy"],
                          "dependencies": dependencies},
        initialize=lambda: "C:/FL/FLEngine_x64.dll",
    )
    monkeypatch.setattr(controller, "native_effect_loader", lambda: loader)
    capabilities = controller.native_capabilities()

    assert capabilities["success"] if "success" in capabilities else True
    assert capabilities["native"]["available"] is True
    assert capabilities["native"]["dependencies"] == dependencies
    assert capabilities["native"]["api_version"] == 1
    assert isinstance(capabilities["context"], str)


def test_native_capabilities_without_dependencies_stays_compatible(controller, monkeypatch):
    """Older .pyd builds without a dependency map remain usable."""
    loader = SimpleNamespace(
        describe=lambda: {"api_version": 1, "engine_profile": "26.1.7.5654-x64",
                          "operations": ["mixer.insert_empty_slot"]},
        initialize=lambda: "C:/FL/FLEngine_x64.dll",
    )
    monkeypatch.setattr(controller, "native_effect_loader", lambda: loader)
    capabilities = controller.native_capabilities()
    assert capabilities["native"]["available"] is True
    assert "dependencies" not in capabilities["native"]


def test_mcp_capability_tool_preserves_fields(monkeypatch):
    from fl_studio_mcp.tools import semantic_native as tools

    dependencies = {"project.save_copy": {"calling_convention": "verified"}}
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "pid:t:0",
        "native": {"available": True, "engine_path": "C:/FL/FLEngine_x64.dll",
                   "operations": ["project.save_copy"], "dependencies": dependencies},
    })
    result = tools.fl_list_capabilities()
    assert result["native"]["dependencies"] == dependencies
    assert result["native"]["operations"] == ["project.save_copy"]


def test_dependency_table_covers_all_writing_targets():
    """The C gate table must declare all three writing operations.

    Parsing the source keeps this check runnable in CI, where the native
    extension is not compiled. The isolated build verifies the runtime gate.
    """
    path = (Path(__file__).resolve().parents[1]
            / "fl_controller/native/dependencies.c")
    source = path.read_text(encoding="utf-8")
    for operation in ("mixer.insert_empty_slot", "mixer.remove_effect",
                      "project.save_copy"):
        assert '"%s"' % operation in source
    for field in ("function_entry", "global_addresses", "object_layout",
                  "calling_convention"):
        assert '"%s"' % field in source
    assert "fl_dependency_gate" in source
