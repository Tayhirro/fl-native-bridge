"""Observed Playlist clip-property editing with exact native tick readback."""

import time

from fl_studio_mcp.utils import desktop
from fl_studio_mcp.utils.file_rpc import FileRPC


def native(rpc, module, method, args=None):
    result = rpc.send("native.call", {"module": module, "method": method, "args": args or []})
    if not result.get("success"):
        raise RuntimeError(result.get("error", "Native clip-state query failed"))
    return result["result"]


def selected_bounds(rpc):
    start = native(rpc, "arrangement", "selectionStart")
    end = native(rpc, "arrangement", "selectionEnd")
    return {
        "start_tick": start,
        "end_tick": end,
        "length_ticks": end - start if start >= 0 and end >= start else None,
        "ppq": native(rpc, "general", "getRecPPQ"),
    }


def refresh_bounds(rpc, playlist_hwnd):
    native(rpc, "ui", "setFocused", [2])
    time.sleep(0.05)
    desktop.input_action(playlist_hwnd, "keys", keys=["ctrl", "enter"])
    return selected_bounds(rpc)


def adjust_clip_time(
    property_hwnd,
    playlist_hwnd,
    before_start_tick,
    before_end_tick,
    start_delta=0,
    duration_delta=0,
    start_point=None,
    duration_point=None,
    rpc=None,
):
    rpc = rpc or FileRPC()
    gui, _, _ = desktop.require_window(property_hwnd)
    desktop.require_window(playlist_hwnd)
    if gui.GetClassName(property_hwnd) != "TPLClipPropForm" or not gui.IsWindowVisible(
        property_hwnd
    ):
        raise ValueError("Observe and open the intended Playlist clip properties first")
    if gui.GetClassName(playlist_hwnd) != "TEventEditForm":
        raise ValueError("Expected the observed Playlist HWND")
    for delta, point in ((start_delta, start_point), (duration_delta, duration_point)):
        if type(delta) is not int or abs(delta) > 100:
            raise ValueError("Each delta must be an integer in -100..100 ticks")
        if delta and (
            not isinstance(point, list) or len(point) != 2 or any(type(v) is not int for v in point)
        ):
            raise ValueError("Each edited field requires its observed physical x/y point")
    if (
        before_start_tick + start_delta < 0
        or before_end_tick - before_start_tick + duration_delta <= 0
    ):
        raise ValueError("Clip start/length would be invalid")
    before = selected_bounds(rpc)
    if (before["start_tick"], before["end_tick"]) != (before_start_tick, before_end_tick):
        raise ValueError("Selection bounds changed; no clip edits submitted")
    if not native(rpc, "general", "safeToEdit"):
        raise RuntimeError("FL is not safe to edit")
    submitted_steps = 0
    try:
        for delta, point in ((start_delta, start_point), (duration_delta, duration_point)):
            # FL's time controls consume the sign of a wheel event, not its
            # magnitude. Ctrl changes the observed quantum from four ticks to one.
            for _ in range(abs(delta)):
                desktop.input_action(
                    property_hwnd,
                    "scroll",
                    x=point[0],
                    y=point[1],
                    dy=1 if delta > 0 else -1,
                    modifiers=["ctrl"],
                )
                submitted_steps += 1
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "submitted_steps": submitted_steps,
            "requires_readback": True,
        }
    # A posted WM_CLOSE can overtake pending system wheel input and redirect it
    # to the Playlist. Keep closure in the same input stream, after an idle read.
    time.sleep(0.2)
    native(rpc, "general", "safeToEdit")
    desktop.input_action(property_hwnd, "keys", keys=["escape"])
    for _ in range(20):
        if not gui.IsWindow(property_hwnd) or not gui.IsWindowVisible(property_hwnd):
            break
        time.sleep(0.05)
    else:
        return {
            "success": False,
            "error": "Property window remained open",
            "submitted_steps": submitted_steps,
            "requires_readback": True,
        }
    observed = refresh_bounds(rpc, playlist_hwnd)
    expected = {
        "start_tick": before_start_tick + start_delta,
        "end_tick": before_end_tick + start_delta + duration_delta,
    }
    verified = all(observed[key] == value for key, value in expected.items())
    return {
        "success": verified,
        "verified_tick_readback": verified,
        "before": before,
        "observed": observed,
        "expected": expected,
        "submitted_steps": submitted_steps,
        "coverage": "One observed clip property editor; not an enumeration of every Playlist clip",
    }


def register_clip_time_tools(mcp):
    @mcp.tool()
    def fl_get_selected_clip_bounds(playlist_hwnd: int) -> dict:
        """Use FL's Select time around selection and read exact ticks. Isolate one clip first; multiple clips return their bounding range."""
        return refresh_bounds(FileRPC(), playlist_hwnd)

    @mcp.tool()
    def fl_adjust_clip_time(
        property_hwnd: int,
        playlist_hwnd: int,
        before_start_tick: int,
        before_end_tick: int,
        start_delta: int = 0,
        duration_delta: int = 0,
        start_point: list[int] | None = None,
        duration_point: list[int] | None = None,
    ) -> dict:
        """Edit an observed clip-property start/length by exact ticks, close that property editor and verify native range. Requires observed field coordinates; never auto-retries."""
        return adjust_clip_time(
            property_hwnd,
            playlist_hwnd,
            before_start_tick,
            before_end_tick,
            start_delta,
            duration_delta,
            start_point,
            duration_point,
        )
