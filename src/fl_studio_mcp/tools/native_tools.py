"""MCP tools for the process-bound native controller bridge."""


def register_native_tools(mcp):
    import math
    import time

    from fl_studio_mcp.utils.connection import get_connection

    def send(action, params=None):
        result = get_connection().send_command(action, params or {})
        if result.get("error") or not result.get("success", False):
            return {
                **result,
                "success": False,
                "error": result.get("error") or "Controller request failed",
            }
        return result

    def native(module, method, args):
        return send("native.call", {"module": module, "method": method, "args": args})

    def verify_later(result, module, method, args, expected):
        if not result.get("success"):
            return result
        for attempt in range(5):
            observed = native(module, method, args)
            if not observed.get("success"):
                return {
                    **result,
                    "success": False,
                    "verified_readback": False,
                    "error": "Write submitted, but subsequent readback failed",
                    "readback": observed,
                }
            value = observed.get("result")
            result.update(
                observed_level=value,
                verified_readback=isinstance(value, (int, float))
                and math.isclose(value, expected, abs_tol=1e-5),
                verification_phase="subsequent_controller_callback",
            )
            if result["verified_readback"]:
                return result
            if attempt < 4:
                time.sleep(0.05)
        return {**result, "success": False, "error": "FL did not confirm the requested value"}

    @mcp.tool()
    def fl_bridge_status() -> dict:
        """Distinguish installed bridge code from live controller activation; list process-bound endpoints."""
        from fl_studio_mcp.utils.file_rpc import EXPECTED_CONTROLLER_BUILD, FileRPC

        rpc = FileRPC()
        heartbeat = rpc.heartbeat()
        return {
            "installed_protocol": 2,
            "active": heartbeat.get("healthy", False),
            "expected_controller_build": EXPECTED_CONTROLLER_BUILD,
            "active_controller_build": heartbeat.get("controller_build"),
            "needs_reload": heartbeat.get("controller_build") != EXPECTED_CONTROLLER_BUILD,
            "selected_endpoint": str(rpc.root),
            "instances": FileRPC.instances(),
            "activation": "Reload FLStudioMCP in FL's Script output, then reconnect the MCP server",
        }

    @mcp.tool()
    def fl_native_capabilities() -> dict:
        """List native functions exposed to FL's embedded controller Python host."""
        return send("native.capabilities")

    @mcp.tool()
    def fl_list_instances() -> dict:
        """List live FL controller endpoints; each FL process has its own queue."""
        from fl_studio_mcp.utils.file_rpc import FileRPC

        return {"instances": FileRPC.instances()}

    @mcp.tool()
    def fl_select_instance(pid: int) -> dict:
        """Bind this MCP server's future commands to one live FL process."""
        from fl_studio_mcp.utils.file_rpc import FileRPC, select_instance

        active = any(instance["pid"] == pid for instance in FileRPC.instances())
        if not active:
            from fl_studio_mcp.utils import desktop

            state = desktop.windows(False, pid=pid)
            if not any(
                window["class_name"] == "TFruityLoopsMainForm" for window in state["windows"]
            ):
                return {
                    "success": False,
                    "error": "No FL main window or live controller endpoint for that PID",
                }
        select_instance(pid)
        return {"success": True, "pid": pid, "controller_active": active}

    @mcp.tool()
    def fl_call_native(
        module: str, method: str, args: list | None = None, kwargs: dict | None = None
    ) -> dict:
        """Call an enabled public FL scripting method. Check capabilities for names/docs and blocked entries; mutations retain safeToEdit protection."""
        return send(
            "native.call",
            {"module": module, "method": method, "args": args or [], "kwargs": kwargs or {}},
        )

    @mcp.tool()
    def fl_native_constants(module: str = "midi") -> dict:
        """Read FL constants (window IDs, command IDs, recording flags and refresh flags)."""
        return send("native.constants", {"module": module})

    @mcp.tool()
    def fl_native_batch(operations: list[dict]) -> dict:
        """Run an ordered batch of native calls. Stops at first error; prior calls are not rolled back."""
        return send("native.batch", {"operations": operations})

    @mcp.tool()
    def fl_get_full_state(start: int = 0, limit: int = 32) -> dict:
        """Read project/transport/window state and a page of mixer, channel and playlist tracks."""
        return send("native.snapshot", {"start": start, "limit": limit})

    @mcp.tool()
    def fl_get_change_events(since: int = 0) -> dict:
        """Read bounded FL refresh/project/channel/mixer event history after a revision."""
        return send("native.events", {"since": since})

    @mcp.tool()
    def fl_get_request_status(request_id: str) -> dict:
        """Resolve an ambiguous timed-out request before repeating an edit."""
        from fl_studio_mcp.utils.file_rpc import FileRPC

        return FileRPC().status(request_id)

    @mcp.tool()
    def fl_native_ui_snapshot() -> dict:
        """Read project title, edit state, form focus and five main form visibility states."""
        return send("native.uiSnapshot")

    @mcp.tool()
    def fl_native_window(window: int, operation: str) -> dict:
        """Show, hide or focus a main FL form (0 mixer, 1 rack, 2 playlist, 3 roll, 4 browser)."""
        return send("native.window", {"window": window, "operation": operation})

    @mcp.tool()
    def fl_native_scroll(window: int, value: int, direction: int = 0) -> dict:
        """Scroll mixer/rack/playlist by FL's semantic item index; not window geometry."""
        return send("native.scroll", {"window": window, "value": value, "direction": direction})

    @mcp.tool()
    def fl_native_mixer_snapshot(index: int) -> dict:
        """Read a mixer's volume, pan, current stereo peaks and enabled send levels."""
        return send("native.mixerSnapshot", {"index": index})

    @mcp.tool()
    def fl_native_set_send_level(source: int, destination: int, level: float) -> dict:
        """Adjust an existing send and report the level read back from FL."""
        return send(
            "native.setSendLevel", {"source": source, "destination": destination, "level": level}
        )

    @mcp.tool()
    def fl_native_set_effect_mix(index: int, slot: int, level: float) -> dict:
        """Set an existing mixer effect slot's wet mix and read it back."""
        result = send("native.setEffectMix", {"index": index, "slot": slot, "level": level})
        return verify_later(result, "mixer", "getPluginMixLevel", [index, slot], level)

    @mcp.tool()
    def fl_set_effect_parameter(
        track: int, slot: int, param_index: int, value: float, allow_mixer_fallback: bool = False
    ) -> dict:
        """Use the public plugin setter with later readback. Optional name-verified Mixer fallback is off by default; never calls general.processRECEvent."""
        if any(type(i) is not int or i < 0 for i in (track, slot, param_index)) or slot > 9:
            raise ValueError("Invalid track, slot or parameter index")
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("value must be finite and normalized to 0..1")
        count = native("mixer", "trackCount", [])
        if not count.get("success"):
            return {"success": False, "error": "Cannot validate the mixer track", "readback": count}
        if track >= count["result"]:
            return {"success": False, "error": "Invalid mixer track"}
        valid = native("plugins", "isValid", [track, slot, True])
        if not valid.get("success") or not valid["result"]:
            return {"success": False, "error": "Invalid or empty effect slot"}
        count = native("plugins", "getParamCount", [track, slot, True])
        if not count.get("success") or param_index >= count["result"]:
            return {"success": False, "error": "Invalid effect parameter index"}
        result = native("plugins", "setParamValue", [value, param_index, track, slot, 0, True])
        result = verify_later(
            result, "plugins", "getParamValue", [param_index, track, slot, True], value
        )
        result.update(
            requested_value=value,
            value=result.get("observed_level"),
            parameter_path="plugins.setParamValue",
        )
        if (
            not allow_mixer_fallback
            or result.get("verified_readback")
            or result.get("error") != "FL did not confirm the requested value"
        ):
            return result
        # Some native mixer effects ignore plugins.setParamValue. Only allow a
        # Mixer event after checking its complete namespace and resolved name.
        location = native("mixer", "getTrackPluginId", [track, slot])
        name = native("plugins", "getParamName", [param_index, track, slot, True])
        if not location.get("success") or not name.get("success"):
            return result
        plugin_id = location["result"]
        if (
            type(plugin_id) is not int
            or plugin_id != ((track << 6) + slot) << 16
            or param_index >= 0x8000
        ):
            return {
                **result,
                "error": "Plugin location ID failed validation; no automation event submitted",
            }
        event_id = plugin_id + 0x70008000 + param_index
        resolved = native("mixer", "getEventIDName", [event_id])
        if (
            not resolved.get("success")
            or not name["result"]
            or name["result"] not in str(resolved.get("result", ""))
        ):
            return {
                **result,
                "error": "Event name does not match the parameter; no automation event submitted",
            }
        result = native("mixer", "automateEvent", [event_id, round(value * (1 << 30)), 1013])
        result = verify_later(
            result, "plugins", "getParamValue", [param_index, track, slot, True], value
        )
        result.update(
            requested_value=value,
            value=result.get("observed_level"),
            parameter_path="mixer.automateEvent",
            event_id=hex(event_id),
            resolved_event_name=resolved["result"],
            mapping_verified=True,
        )
        return result

    @mcp.tool()
    def fl_native_clone_pattern(index: int) -> dict:
        """Clone one pattern using the native API; persistence requires a project save/reopen test."""
        return send("native.clonePattern", {"index": index})

    @mcp.tool()
    def fl_native_rename_pattern(index: int, name: str) -> dict:
        """Rename a pattern and read the name back."""
        return send("native.renamePattern", {"index": index, "name": name})
