"""Regression: effect-loading tools must forward the pinned context to the loader.

The controller rejects `mixer.loadEffectAtSlot` with "The FL controller or
current project changed before insertion" when `context` is None or stale.
`fl_load_plugin_preset` and `fl_load_effect_at_slot` both read capabilities
but used to call the shared loader without forwarding context/engine_path:
dry_run returned before the write so it looked fine, while every real load
failed. These tests patch the shared loader and assert the context arrives.
"""

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fl_studio_mcp.tools.semantic_native as tools  # noqa: E402

CONTEXT = "1234:1795702040090673100:0"
ENGINE = "C:/fl/FL64.exe"


def test_plugin_preset_forwards_context_to_loader(monkeypatch):
    calls = []

    def fake_loader(conn, name, track, slot, dry_run, preset_path=None, **kwargs):
        calls.append(kwargs)
        return {"success": True, "submitted": True, "verified": True}

    monkeypatch.setattr(tools, "load_effect_at_slot", fake_loader)
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": CONTEXT,
        "native": {"available": True, "engine_path": ENGINE}})
    class FakePreset:
        stem = "Default"
        def stat(self): return type("S", (), {"st_size": 10})()
        def __str__(self): return "C:/presets/Default.fst"

    monkeypatch.setattr(tools, "_checked_effect_preset", lambda p: FakePreset())
    monkeypatch.setattr(tools, "_preset_roots", lambda engine, kind: [])
    monkeypatch.setattr(Path, "is_absolute", lambda self: True)

    tools.fl_load_plugin_preset("Fruity Delay 3", "Default", 3, 5)
    assert calls, "loader was not called"
    assert calls[0].get("context") == CONTEXT, (
        "fl_load_plugin_preset calls the loader without context; the real "
        "controller rejects this with 'current project changed before insertion'"
    )
    assert calls[0].get("engine_path") == ENGINE


def test_effect_at_slot_tool_source_forwards_context():
    """The registered fl_load_effect_at_slot tool must pin context too."""
    src = inspect.getsource(tools.register_semantic_native_tools)
    assert "caps = fl_list_capabilities()" in src, (
        "fl_load_effect_at_slot does not read capabilities for context"
    )
    assert "context=caps[\"context\"]" in src, (
        "fl_load_effect_at_slot does not forward context to the loader"
    )
    assert "engine_path=caps[\"native\"][\"engine_path\"]" in src
