from types import SimpleNamespace

import pytest

from fl_studio_mcp.tools import clip_time


def setup_clip(monkeypatch, fail_scroll=False):
    state = {"start": 100, "end": 292, "visible": True, "events": []}

    class RPC:
        def send(self, action, params):
            method = params["method"]
            if method == "selectionStart":
                value = state["start"]
            elif method == "selectionEnd":
                value = state["end"]
            elif method == "getRecPPQ":
                value = 96
            elif method == "safeToEdit":
                state["events"].append("idle_check")
                value = True
            elif method == "setFocused":
                value = None
            else:
                raise AssertionError(method)
            return {"success": True, "result": value}

    gui = SimpleNamespace(
        GetClassName=lambda h: "TPLClipPropForm" if h == 1 else "TEventEditForm",
        IsWindowVisible=lambda h: state["visible"] if h == 1 else True,
        IsWindow=lambda h: True,
    )
    monkeypatch.setattr(clip_time.desktop, "require_window", lambda h: (gui, None, 123))
    monkeypatch.setattr(clip_time.time, "sleep", lambda _: None)

    def input_action(hwnd, action, **params):
        if action == "scroll":
            assert params["modifiers"] == ["ctrl"] and abs(params["dy"]) == 1
            if fail_scroll and "scroll" in state["events"]:
                raise RuntimeError("Input outcome uncertain")
            state["events"].append("scroll")
            if params["y"] == 10:
                state["start"] += params["dy"]
                state["end"] += params["dy"]
            else:
                state["end"] += params["dy"]
        elif params["keys"] == ["escape"]:
            assert state["events"][-1] == "idle_check"
            state["events"].append("ordered_close")
            state["visible"] = False

    monkeypatch.setattr(clip_time.desktop, "input_action", input_action)
    return state, RPC()


def test_clip_tick_edit_uses_unit_events_and_closes_through_input_order(monkeypatch):
    state, rpc = setup_clip(monkeypatch)
    result = clip_time.adjust_clip_time(1, 2, 100, 292, 1, 2, [10, 10], [10, 20], rpc)
    assert result["verified_tick_readback"]
    assert result["observed"]["start_tick"] == 101 and result["observed"]["end_tick"] == 295
    assert state["events"].count("scroll") == 3 and not state["visible"]


def test_changed_selection_does_not_submit_input(monkeypatch):
    state, rpc = setup_clip(monkeypatch)
    with pytest.raises(ValueError, match="Selection bounds changed"):
        clip_time.adjust_clip_time(1, 2, 99, 292, 1, 0, [10, 10], None, rpc)
    assert not state["events"] and state["visible"]


def test_partial_input_is_reported_without_closing_or_replaying(monkeypatch):
    state, rpc = setup_clip(monkeypatch, fail_scroll=True)
    result = clip_time.adjust_clip_time(1, 2, 100, 292, 2, 0, [10, 10], None, rpc)
    assert not result["success"] and result["requires_readback"]
    assert result["submitted_steps"] == 1 and state["visible"]
