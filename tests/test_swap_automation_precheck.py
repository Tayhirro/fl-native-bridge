"""Stage-4 acceptance: automation-target precheck for structural FX writes.

The post-swap resolution of a location-encoded event ID has not been measured
on this build, so a swap must refuse when an affected link cannot be confirmed
against a currently valid plugin parameter. Confirmed links are reported as a
precheck only, never claimed as verified post-swap behaviour.
"""

import sys
import types
from pathlib import Path

import pytest

from fl_studio_mcp.tools import semantic_native as tools

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fx_compound_writes import Harness, uuid  # noqa: E402


@pytest.fixture
def harness(monkeypatch, tmp_path):
    return Harness(monkeypatch, tmp_path)


def _scan_response(links, *, success=True):
    return {"success": success, "native_request_id": uuid(),
            "queue_request_id": uuid(), "context": "1234:1000:0",
            "affected": links, "channel_count": 3,
            "post_swap_resolution_verified": False}


def _inject_scan(harness, response):
    original = harness.send_command

    def guarded(action, params, timeout=10):
        if action == "native.scanMixerAutomationLinks":
            harness.calls.append((action, params))
            return response
        return original(action, params, timeout)

    harness.monkeypatch.setattr(
        tools, "get_connection",
        lambda: types.SimpleNamespace(send_command=guarded))


def _confirmed_link(slot=5):
    return {"channel": 2, "source_event": 1879056384,
            "target_event_id": 0x70008000 + (3 * 64 + slot) * 0x10000 + 1,
            "track": 3, "slot": slot, "parameter": 1,
            "binding_confirmed": True,
            "target": {"kind": "mixer_effect_parameter", "track": 3, "slot": slot,
                       "parameter": 1, "plugin_name": "Fruity Delay 3",
                       "parameter_name": "Mix"}}


def _unconfirmed_link(slot=5):
    link = _confirmed_link(slot)
    link["binding_confirmed"] = False
    link["target"] = None
    return link


def test_swap_refuses_unconfirmed_automation_binding(harness):
    _inject_scan(harness, _scan_response([_unconfirmed_link()]))
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is False
    assert result["state"] == "not_written"
    assert "automation link" in result["error"]
    assert not [a for a, _ in harness.calls if a == "native.removeMixerEffect"]
    assert result["automation_precheck"]["post_swap_resolution_verified"] is False


def test_swap_reports_confirmed_binding_but_does_not_claim_post_resolution(harness):
    _inject_scan(harness, _scan_response([_confirmed_link()]))
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is True
    precheck = result["automation_precheck"]
    assert precheck["available"] is True
    assert len(precheck["affected_links"]) == 1
    assert precheck["affected_links"][0]["target"]["parameter_name"] == "Mix"
    assert precheck["post_swap_resolution_verified"] is False


def test_swap_refuses_when_automation_scan_unavailable(harness):
    _inject_scan(harness, _scan_response([], success=False))
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["state"] == "not_written"
    assert result["automation_precheck"]["available"] is False
    assert not [a for a, _ in harness.calls if a == "native.removeMixerEffect"]


def test_scan_only_covers_requested_slots(harness):
    """The precheck request must name exactly the two slots being swapped."""
    _inject_scan(harness, _scan_response([]))
    tools.fl_swap_mixer_effects(3, 5, 7)
    call = next(a for a, p in harness.calls if a == "native.scanMixerAutomationLinks")
    params = next(p for a, p in harness.calls if a == "native.scanMixerAutomationLinks")
    assert call == "native.scanMixerAutomationLinks"
    assert params["track"] == 3
    assert params["slots"] == [5, 7]


def test_point_edit_reports_requested_value_and_binding(monkeypatch):
    """A point edit reports its request value, readback and binding info."""
    import struct

    snap = "1234:1000:0:5:1879056384:2:0:" + (struct.pack("<ffff", 0, 0.5, 0, 0) * 2).hex()
    calls = []

    class Conn:
        def send_command(self, action, params, timeout=10):
            calls.append((action, params))
            if action == "native.setAutomationPointValue":
                return {"success": True, "submitted": True,
                        "native_request_id": params["request_id"]}
            if action == "native.readAutomationLinks":
                return {"success": True, "links": [{
                    "source_event": 1879056384,
                    "target": {"kind": "mixer_effect_parameter", "track": 3,
                               "slot": 5, "parameter": 1, "parameter_name": "Mix"},
                }]}
            return {"success": False}

    monkeypatch.setattr(tools, "get_connection", lambda: Conn())
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "1234:1000:0",
        "native": {"available": True, "operations": ["automation.read_links"]}})
    # The readback path uses fl_list_automation_points; feed a matching record.
    updated = struct.pack("<ffff", 0, 0.25, 0, 0)
    monkeypatch.setattr(tools, "fl_list_automation_points", lambda **kw: {
        "success": True, "context": "1234:1000:0", "source_event": 1879056384,
        "points": [{"index": 2, "value": 0.25,
                    "snapshot_ref": "1234:1000:0:5:1879056384:2:0x0:" + updated.hex()}]})

    result = tools.fl_set_automation_point_value(snap, 0.25)
    assert result["requested_value"] == 0.25
    assert result["verified"] is True
    assert result["binding"] is not None
    assert result["binding"]["parameter_name"] == "Mix"
    assert result["observed"]["value"] == 0.25


def test_controller_scan_has_channels_import(monkeypatch):
    """The controller scan must not fail with NameError on channels.

    Regression: scan_mixer_automation_links called channels.channelCount()
    without importing the FL host module inside the function.
    """
    import importlib.util
    import types

    spec = importlib.util.spec_from_file_location(
        "fl_ctrl_scan", Path(__file__).resolve().parents[1]
        / "fl_controller" / "semantic_native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "effect_context", lambda: "ctx-A")
    channels_stub = types.SimpleNamespace(channelCount=lambda _: 0)
    monkeypatch.setitem(sys.modules, "channels", channels_stub)
    monkeypatch.setattr(module, "native_effect_loader", lambda: types.SimpleNamespace(
        initialize=lambda: "engine-path"))
    # __import__ inside the function resolves through sys.modules.

    result = module.scan_mixer_automation_links({
        "context": "ctx-A", "track": 3, "slots": [5, 7]})
    assert result["success"] is True
    assert result["affected"] == []
