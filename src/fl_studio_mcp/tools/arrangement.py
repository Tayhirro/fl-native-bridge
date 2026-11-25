"""Arrangement/Playlist tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_arrangement_tools(mcp: FastMCP) -> None:
    """Register arrangement and playlist tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool()
    def fl_get_arrangement_selection() -> dict:
        """Get the current selection range in the arrangement/playlist view.

        Returns start and end positions of the current selection.
        """
        conn = get_connection()
        result = conn.send_command("arrangement.getSelection")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {
            "start": result.get("start", 0),
            "end": result.get("end", 0),
            "time_start": result.get("time_start", 0),
        }

    @mcp.tool()
    def fl_list_arrangement_methods() -> dict:
        """List all available FL Studio arrangement API methods.

        Returns a list of callable method names on the arrangement module.
        Use this to discover what arrangement operations are available.
        """
        conn = get_connection()
        result = conn.send_command("arrangement.listMethods")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {"methods": result.get("methods", [])}

    @mcp.tool()
    def fl_list_playlist_methods() -> dict:
        """List all available FL Studio playlist API methods.

        Returns a list of callable method names on the playlist module.
        Use this to discover what playlist operations are available.
        """
        conn = get_connection()
        result = conn.send_command("playlist.listMethods")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {"methods": result.get("methods", [])}

    @mcp.tool()
    def fl_list_ui_methods() -> dict:
        """List all available FL Studio UI API methods.

        Returns a list of callable method names on the ui module.
        """
        conn = get_connection()
        result = conn.send_command("ui.listMethods")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {"methods": result.get("methods", [])}

    @mcp.tool()
    def fl_list_general_methods() -> dict:
        """List all available FL Studio general API methods.

        Returns a list of callable method names on the general module.
        """
        conn = get_connection()
        result = conn.send_command("general.listMethods")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {"methods": result.get("methods", [])}

    @mcp.tool()
    def fl_list_patterns_methods() -> dict:
        """List all available FL Studio patterns API methods.

        Returns a list of callable method names on the patterns module.
        """
        conn = get_connection()
        result = conn.send_command("patterns.listMethods")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {"methods": result.get("methods", [])}

    @mcp.tool()
    def fl_list_device_methods() -> dict:
        """List all available FL Studio device API methods.

        Returns a list of callable method names on the device module.
        """
        conn = get_connection()
        result = conn.send_command("device.listMethods")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {"methods": result.get("methods", [])}

    # ── Playlist track tools ──

    @mcp.tool()
    def fl_get_playlist_track_count() -> dict:
        """Get the total number of tracks in the FL Studio playlist."""
        conn = get_connection()
        result = conn.send_command("playlist.trackCount")
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"count": result.get("count", 0)}

    @mcp.tool()
    def fl_get_playlist_track_name(index: int) -> dict:
        """Get the name of a playlist track by index (0-based)."""
        conn = get_connection()
        result = conn.send_command("playlist.getTrackName", {"index": index})
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": result.get("index"), "name": result.get("name")}

    @mcp.tool()
    def fl_is_playlist_track_selected(index: int) -> dict:
        """Check if a playlist track is currently selected."""
        conn = get_connection()
        result = conn.send_command("playlist.isTrackSelected", {"index": index})
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": result.get("index"), "selected": result.get("selected")}

    @mcp.tool()
    def fl_select_playlist_track(index: int) -> dict:
        """Select a playlist track by index (0-based)."""
        conn = get_connection()
        result = conn.send_command("playlist.selectTrack", {"index": index})
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": result.get("index"), "name": result.get("name")}

    @mcp.tool()
    def fl_get_playlist_track_color(index: int) -> dict:
        """Get the color of a playlist track by index."""
        conn = get_connection()
        result = conn.send_command("playlist.getTrackColor", {"index": index})
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": result.get("index"), "color": result.get("color")}

    @mcp.tool()
    def fl_get_all_playlist_tracks() -> dict:
        """Get info about all playlist tracks (name, selected, color)."""
        conn = get_connection()
        count_r = conn.send_command("playlist.trackCount")
        if not count_r.get("success") and "error" in count_r:
            return {"error": count_r["error"]}
        count = count_r.get("count", 0)
        tracks = []
        for i in range(count):
            name_r = conn.send_command("playlist.getTrackName", {"index": i})
            sel_r = conn.send_command("playlist.isTrackSelected", {"index": i})
            color_r = conn.send_command("playlist.getTrackColor", {"index": i})
            tracks.append({
                "index": i,
                "name": name_r.get("name", ""),
                "selected": sel_r.get("selected", False),
                "color": color_r.get("color", ""),
            })
        return {"tracks": tracks}

    @mcp.tool()
    def fl_is_playlist_track_muted(index: int) -> dict:
        """Check if a playlist track is muted."""
        conn = get_connection()
        result = conn.send_command("playlist.isTrackMuted", {"index": index})
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": index, "muted": result.get("muted")}

    @mcp.tool()
    def fl_mute_playlist_track(index: int, muted: bool = None) -> dict:
        """Mute or unmute a playlist track. Omit muted to toggle."""
        conn = get_connection()
        params = {"index": index}
        if muted is not None:
            params["muted"] = muted
        result = conn.send_command("playlist.muteTrack", params)
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": index, "muted": result.get("muted")}

    @mcp.tool()
    def fl_is_playlist_track_solo(index: int) -> dict:
        """Check if a playlist track is soloed."""
        conn = get_connection()
        result = conn.send_command("playlist.isTrackSolo", {"index": index})
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": index, "solo": result.get("solo")}

    @mcp.tool()
    def fl_solo_playlist_track(index: int, solo: bool = None) -> dict:
        """Solo or unsolo a playlist track. Omit solo to toggle."""
        conn = get_connection()
        params = {"index": index}
        if solo is not None:
            params["solo"] = solo
        result = conn.send_command("playlist.soloTrack", params)
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": index, "solo": result.get("solo")}

    # ── Pattern tools ──

    @mcp.tool()
    def fl_get_pattern_count() -> dict:
        """Get the total number of patterns in the project."""
        conn = get_connection()
        result = conn.send_command("patterns.patternCount")
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"count": result.get("count", 0)}

    @mcp.tool()
    def fl_get_current_pattern() -> dict:
        """Get the current pattern number and name."""
        conn = get_connection()
        num_r = conn.send_command("patterns.patternNumber")
        if num_r.get("success"):
            num = num_r.get("number", 0)
            name_r = conn.send_command("patterns.getPatternName", {"index": num})
            return {"number": num, "name": name_r.get("name", "")}
        return {"error": num_r.get("error", "unknown")}

    @mcp.tool()
    def fl_get_pattern_name(index: int) -> dict:
        """Get the name of a pattern by index."""
        conn = get_connection()
        result = conn.send_command("patterns.getPatternName", {"index": index})
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": result.get("index"), "name": result.get("name")}

    @mcp.tool()
    def fl_jump_to_pattern(index: int) -> dict:
        """Jump to a pattern by index."""
        conn = get_connection()
        result = conn.send_command("patterns.jumpToPattern", {"index": index})
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": result.get("index"), "name": result.get("name")}

    @mcp.tool()
    def fl_get_pattern_length(index: int) -> dict:
        """Get the length of a pattern in beats."""
        conn = get_connection()
        result = conn.send_command("patterns.getPatternLength", {"index": index})
        if not result.get("success") and "error" in result:
            return {"error": result["error"]}
        return {"index": result.get("index"), "length": result.get("length")}
