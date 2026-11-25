"""Native Windows desktop tools and asset workflows for FL Studio."""

from pathlib import Path
import time


def register_desktop_tools(mcp):
    from fl_studio_mcp.utils import desktop
    from fl_studio_mcp.utils.file_rpc import FileRPC

    @mcp.tool()
    def fl_list_windows(include_controls: bool = False) -> dict:
        """List FL form HWNDs, parent hierarchy, DPI and physical pixel geometry."""
        return desktop.windows(include_controls)

    @mcp.tool()
    def fl_move_window(
        hwnd: int, x: int, y: int, width: int | None = None, height: int | None = None
    ) -> dict:
        """Move/resize a visible FL form using physical screen pixels; read back actual geometry."""
        return desktop.move(hwnd, x, y, width, height)

    @mcp.tool()
    def fl_focus_window(hwnd: int) -> dict:
        """Focus a visible FL form. Hidden forms must be opened through FL's native API."""
        return desktop.focus(hwnd)

    @mcp.tool()
    def fl_desktop_action(hwnd: int, operation: str, params: dict) -> dict:
        """Send click/drag/scroll/keys/text to the foreground FL instance. Observe after each action. Coordinates are physical screen pixels."""
        return desktop.input_action(hwnd, operation, **params)

    @mcp.tool()
    def fl_screenshot(hwnd: int):
        """Capture a visible FL HWND without foreground activation or forced repaint; background pixels can be cached."""
        from fastmcp.utilities.types import Image

        output = (
            Path.home()
            / "Documents/Image-Line/FL Studio/Settings/Hardware/FLStudioMCP/captures"
            / (str(time.time_ns()) + ".png")
        )
        desktop.screenshot(hwnd, output)
        return Image(path=output)

    @mcp.tool()
    def fl_drop_asset(hwnd: int, path: str, x: int, y: int) -> dict:
        """Drop an audio/MIDI/preset file using Windows OLE. Submission is not proof of import; inspect native state afterward."""
        return desktop.drop_file(hwnd, path, x, y)

    @mcp.tool()
    def fl_plugin_catalog(query: str = "", kind: str = "all", limit: int = 100) -> dict:
        """Find installed FL plugin database presets by name; paths can be used for native OLE drops."""
        if kind not in ("all", "effects", "generators") or not 1 <= limit <= 500:
            raise ValueError("kind must be all/effects/generators; limit must be 1..500")
        executable = desktop.executable_path(desktop.target_pid())
        roots = [
            executable.parent / "Data/Patches/Plugin database",
            Path.home() / "Documents/Image-Line/FL Studio/Presets/Plugin database",
        ]
        results = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.fst"):
                relative = str(path.relative_to(root))
                if query.casefold() not in relative.casefold():
                    continue
                if kind != "all" and kind not in relative.casefold():
                    continue
                results.append({"name": path.stem, "path": str(path), "category": relative})
        results.sort(key=lambda item: item["category"].casefold())
        return {
            "matches": results[:limit],
            "total_matches": len(results),
            "truncated": len(results) > limit,
        }

    @mcp.tool()
    def fl_import_audio(
        path: str, hwnd: int, x: int, y: int, mixer_track: int | None = None
    ) -> dict:
        """Drop audio onto an observed empty Playlist location; verify new channels and optionally route them to a mixer insert. Does not assume clip position from pixels."""
        file = Path(path).resolve(strict=True)
        if file.suffix.lower() not in (".wav", ".aif", ".aiff", ".mp3", ".flac", ".ogg"):
            raise ValueError("Expected an audio file")
        rpc = FileRPC()

        def call(module, method, args=None):
            response = rpc.send(
                "native.call", {"module": module, "method": method, "args": args or []}
            )
            if not response.get("success"):
                raise RuntimeError(response.get("error", "Native call failed"))
            return response["result"]

        def channel_names(count):
            names = []
            for start in range(0, count, 128):
                response = rpc.send(
                    "native.batch",
                    {
                        "operations": [
                            dict(module="channels", method="getChannelName", args=[i, True])
                            for i in range(start, min(start + 128, count))
                        ]
                    },
                )
                if not response.get("success"):
                    raise RuntimeError(response.get("error", "Channel identity readback failed"))
                names.extend(item["result"] for item in response["results"])
            return names

        before = call("channels", "channelCount", [True])
        before_names = channel_names(before)
        if mixer_track is not None and (
            type(mixer_track) is not int or not 0 <= mixer_track < call("mixer", "trackCount")
        ):
            raise ValueError("Invalid mixer track")
        submission = desktop.drop_file(hwnd, str(file), x, y)
        if not submission["submitted"]:
            return {
                **submission,
                "success": False,
                "error": submission.get("error", "Windows reported no accepted drop"),
                "drop": submission,
            }
        deadline = time.monotonic() + 8
        after = before
        while time.monotonic() < deadline:
            after = call("channels", "channelCount", [True])
            if after > before:
                break
            time.sleep(0.1)
        added = []
        if channel_names(after)[:before] != before_names:
            return {
                "success": False,
                "submitted": True,
                "error": "Existing channel order changed; automatic routing was skipped",
                "verified_new_channels": False,
                "requires_playlist_observation": True,
            }
        for index in range(before, after):
            if mixer_track is not None:
                call("channels", "setTargetFxTrack", [index, mixer_track, True])
            added.append(
                {
                    "index": index,
                    "name": call("channels", "getChannelName", [index, True]),
                    "mixer_track": call("channels", "getTargetFxTrack", [index, True]),
                }
            )
        return {
            "submitted": True,
            "new_channels": added,
            "verified_new_channels": bool(added),
            "channel_index_space": "global",
            "verified_routing": bool(added)
            and mixer_track is not None
            and all(item["mixer_track"] == mixer_track for item in added),
            "verified_clip_position": False,
            "requires_playlist_observation": True,
        }

    @mcp.tool()
    def fl_load_effect_preset(
        preset_path: str, track: int, slot: int, hwnd: int, x: int, y: int
    ) -> dict:
        """Load an effect preset at an observed Mixer slot drop location and read back the slot identity; may replace an existing effect."""
        file = Path(preset_path).resolve(strict=True)
        if file.suffix.lower() not in (".fst", ".fxp", ".fxb", ".vstpreset"):
            raise ValueError("Expected a plugin preset file")
        rpc = FileRPC()

        def call(module, method, args):
            response = rpc.send("native.call", {"module": module, "method": method, "args": args})
            if not response.get("success"):
                raise RuntimeError(response.get("error", "Native call failed"))
            return response["result"]

        if (
            type(track) is not int
            or not 0 <= track < call("mixer", "trackCount", [])
            or type(slot) is not int
            or not 0 <= slot <= 9
        ):
            raise ValueError("Invalid mixer track or effect slot")
        before = (
            call("plugins", "getPluginName", [track, slot])
            if call("plugins", "isValid", [track, slot])
            else None
        )
        call("mixer", "setActiveTrack", [track])
        call("mixer", "setTrackNumber", [track, 1])
        if call("mixer", "trackNumber", []) != track:
            raise RuntimeError("FL did not select the requested mixer track; no drop was sent")
        call("ui", "showWindow", [0])
        call("ui", "setFocused", [0])
        time.sleep(0.2)
        if call("mixer", "trackNumber", []) != track:
            raise RuntimeError("Mixer selection changed before the drop; no file was submitted")
        submission = desktop.drop_file(hwnd, str(file), x, y)
        after = None
        deadline = time.monotonic() + 10
        while submission["submitted"] and time.monotonic() < deadline:
            if call("plugins", "isValid", [track, slot]):
                after = call("plugins", "getPluginName", [track, slot])
                if after != before:
                    break
            time.sleep(0.1)
        return {
            "submitted": submission["submitted"],
            "track": track,
            "slot": slot,
            "before": before,
            "after": after,
            "selected_track_after_drop": call("mixer", "trackNumber", []),
            "drop": submission,
            "verified_identity_changed": after is not None and after != before,
            "verified_preset_state": False,
        }
