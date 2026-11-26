"""Paged public plugin parameter state, with identity checks before restoration."""

import math
import time

from fl_studio_mcp.utils.file_rpc import FileRPC


class PluginParameters:
    def __init__(self, rpc=None):
        self.rpc = rpc or FileRPC()

    def call(self, method, args):
        result = self.rpc.send("native.call", {"module": "plugins", "method": method, "args": args})
        if not result.get("success"):
            raise RuntimeError(result.get("error", "Plugin call failed"))
        return result["result"]

    def batch(self, operations):
        result = self.rpc.send("native.batch", {"operations": operations})
        if not result.get("success"):
            raise RuntimeError(
                result.get("error", "Parameter batch partially failed; inspect current state")
            )
        return [item["result"] for item in result["results"]]

    def identity(self, index, slot):
        if type(index) is not int or index < 0 or type(slot) is not int or not -1 <= slot <= 9:
            raise ValueError("Expected a global channel index or mixer index, and slot -1..9")
        if not self.call("isValid", [index, slot, True]):
            raise ValueError("Plugin is empty or invalid")
        name = self.call("getPluginName", [index, slot, False, True])
        count = self.call("getParamCount", [index, slot, True])
        if type(count) is not int or not 0 <= count <= 65536:
            raise ValueError("Unexpected parameter count")
        return {"index": index, "slot": slot, "plugin_name": name, "parameter_count": count}

    def capture(self, index, slot=-1, start=0, limit=64):
        identity = self.identity(index, slot)
        if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("start must be nonnegative; limit must be 1..64")
        operations = []
        indices = range(start, min(start + limit, identity["parameter_count"]))
        for parameter in indices:
            for method in ("getParamName", "getParamValue"):
                operations.append(
                    {"module": "plugins", "method": method, "args": [parameter, index, slot, True]}
                )
        values = self.batch(operations) if operations else []
        parameters = [
            {"index": parameter, "name": values[offset * 2], "value": values[offset * 2 + 1]}
            for offset, parameter in enumerate(indices)
        ]
        if self.identity(index, slot) != identity:
            raise RuntimeError("Plugin identity changed during capture")
        return {
            "schema": "fl-public-plugin-parameters/v1",
            "target": identity,
            "captured_at": time.time(),
            "parameters": parameters,
            "page": {"start": start, "limit": limit, "next_start": start + len(parameters)},
            "complete": start == 0 and len(parameters) == identity["parameter_count"],
            "atomic": False,
            "coverage": "Public parameters only; opaque preset/sample/editor state is separate",
        }

    def capture_all(self, index, slot=-1):
        result = self.capture(index, slot)
        while len(result["parameters"]) < result["target"]["parameter_count"]:
            page = self.capture(index, slot, len(result["parameters"]), 64)
            if page["target"] != result["target"]:
                raise RuntimeError("Plugin changed between parameter pages")
            result["parameters"].extend(page["parameters"])
        result.update(complete=True, page=None)
        return result

    def restore(self, state):
        if not isinstance(state, dict) or state.get("schema") != "fl-public-plugin-parameters/v1":
            raise ValueError("Unsupported parameter state schema")
        target = state.get("target", {})
        identity = self.identity(target.get("index"), target.get("slot"))
        if identity != target:
            raise ValueError(
                "Plugin name, location or parameter count differs; no parameters written"
            )
        return self._write_named(state, identity)

    def restore_to(self, state, target_index, target_slot):
        """Write captured named parameters into a DIFFERENT instance of the same plugin.

        Cross-slot cloning cannot use restore(): its identity check includes the
        source location. This variant keeps the plugin-name and parameter-count
        checks but accepts a new index/slot. It writes named public parameters
        only; opaque preset/sample/editor state is not cloned.
        """
        if not isinstance(state, dict) or state.get("schema") != "fl-public-plugin-parameters/v1":
            raise ValueError("Unsupported parameter state schema")
        source = state.get("target", {})
        identity = self.identity(target_index, target_slot)
        if (identity["plugin_name"] != source.get("plugin_name")
                or identity["parameter_count"] != source.get("parameter_count")):
            raise ValueError(
                "Target plugin is not the same model with the same parameter count; "
                "no parameters written"
            )
        return self._write_named(state, identity)

    def _write_named(self, state, identity):
        parameters = state.get("parameters")
        if not isinstance(parameters, list) or len(parameters) > identity["parameter_count"]:
            raise ValueError("Invalid parameter list")
        seen = set()
        for parameter in parameters:
            index, value = parameter.get("index"), parameter.get("value")
            if (
                type(index) is not int
                or not 0 <= index < identity["parameter_count"]
                or index in seen
            ):
                raise ValueError("Invalid or duplicate parameter index")
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("Parameter values must be finite and normalized to 0..1")
            if not isinstance(parameter.get("name"), str):
                raise ValueError("Missing parameter name")
            seen.add(index)

        # VST wrappers enumerate thousands of unused slots. A real preset reload
        # changed an unnamed slot although every named control was restored.
        # Never write an index without a name/meaning confirmed by the host.
        skipped_unnamed = sum(not parameter["name"] for parameter in parameters)
        parameters = [parameter for parameter in parameters if parameter["name"]]

        # Validate every captured name/value before submitting the first write.
        changed = []
        for start in range(0, len(parameters), 64):
            page = parameters[start : start + 64]
            operations = []
            for parameter in page:
                for method in ("getParamName", "getParamValue"):
                    operations.append(
                        {
                            "module": "plugins",
                            "method": method,
                            "args": [parameter["index"], identity["index"], identity["slot"], True],
                        }
                    )
            observed = self.batch(operations)
            for offset, parameter in enumerate(page):
                if observed[offset * 2] != parameter["name"]:
                    raise ValueError("Parameter name differs; no parameters written")
                if not math.isclose(observed[offset * 2 + 1], parameter["value"], abs_tol=1e-5):
                    if not parameter["name"]:
                        raise ValueError("Unnamed parameter changed; no parameters written")
                    changed.append(parameter)

        verified = []
        for parameter in changed:
            if self.identity(identity["index"], identity["slot"]) != identity:
                return {"success": False, "error": "Plugin identity changed", "verified": verified}
            self.call(
                "setParamValue",
                [
                    parameter["value"],
                    parameter["index"],
                    identity["index"],
                    identity["slot"],
                    0,
                    True,
                ],
            )
            matched = False
            for attempt in range(5):
                value = self.call(
                    "getParamValue", [parameter["index"], identity["index"], identity["slot"], True]
                )
                matched = math.isclose(value, parameter["value"], abs_tol=1e-5)
                if matched:
                    break
                if attempt < 4:
                    time.sleep(0.05)
            if not matched:
                return {
                    "success": False,
                    "error": "Parameter did not confirm; remaining writes stopped",
                    "failed_index": parameter["index"],
                    "verified": verified,
                    "observed_value": value,
                }
            verified.append(parameter["index"])
        return {
            "success": True,
            "verified_readback": True,
            "changed_count": len(changed),
            "verified": verified,
            "transactional": False,
            "skipped_unnamed": skipped_unnamed,
            "restore_scope": "Named public parameters only",
            "coverage": state.get("coverage"),
        }


class _SendCommandAdapter:
    """Expose FileRPC.send as send_command for the shared effect loader."""
    def __init__(self, rpc):
        self.rpc = rpc

    def send_command(self, action, params=None, timeout=10):
        return self.rpc.send(action, params or {})


def clone_mixer_effect(rpc, source_track, source_slot, target_track, target_slot, dry_run=False):
    """Clone named public parameters between Mixer effect slots (see MCP tool doc)."""
    if any(type(v) is not int for v in (source_track, source_slot, target_track, target_slot)) \
            or not 0 <= source_track <= 500 or not 0 <= target_track <= 500 \
            or not 0 <= source_slot <= 9 or not 0 <= target_slot <= 9 \
            or type(dry_run) is not bool:
        return {"success": False, "error": "Invalid track/slot indices or dry_run"}
    if (source_track, source_slot) == (target_track, target_slot):
        return {"success": False, "error": "Source and target slot are the same"}
    rpc = rpc or FileRPC()
    parameters = PluginParameters(rpc)
    try:
        source_identity = parameters.identity(source_track, source_slot)
        state = parameters.capture_all(source_track, source_slot)
    except (RuntimeError, ValueError) as exc:
        return {"success": False, "error": "Cannot capture source: " + str(exc)}
    plan = {
        "success": True, "submitted": False, "dry_run": dry_run,
        "source": source_identity,
        "target": {"track": target_track, "slot": target_slot},
        "named_parameters": sum(1 for p in state["parameters"] if p["name"]),
        "skipped_unnamed": sum(1 for p in state["parameters"] if not p["name"]),
        "coverage": "Named public parameters only; opaque state not cloned",
    }
    if dry_run:
        return plan
    try:
        if parameters.call("isValid", [target_track, target_slot, True]):
            return {"success": False, "error": "Target slot is occupied"}
    except (RuntimeError, ValueError) as exc:
        return {"success": False, "error": "Cannot read target: " + str(exc)}
    from fl_studio_mcp.tools.semantic_native import load_effect_at_slot
    loaded = load_effect_at_slot(_SendCommandAdapter(rpc), source_identity["plugin_name"],
                                 target_track, target_slot, False)
    if not loaded.get("verified"):
        return {"success": False, "error": loaded.get("error") or "Target plugin load failed"}
    try:
        writeback = parameters.restore_to(state, target_track, target_slot)
    except (RuntimeError, ValueError) as exc:
        return {"success": False, "submitted": True,
                "error": "Target loaded but parameter writeback failed: " + str(exc)}
    return {**plan, "submitted": True, "writeback": writeback,
            "success": bool(writeback.get("success"))}


def register_plugin_state_tools(mcp):
    @mcp.tool()
    def fl_get_plugin_parameter_state(
        index: int, slot: int = -1, start: int = 0, limit: int = 64
    ) -> dict:
        """Read a global channel/mixer plugin's public parameter names and values in pages; opaque state is separate."""
        return PluginParameters().capture(index, slot, start, limit)

    @mcp.tool()
    def fl_capture_plugin_parameter_state(index: int, slot: int = -1) -> dict:
        """Capture all publicly enumerated parameters, including unused wrapper entries; no project save."""
        return PluginParameters().capture_all(index, slot)

    @mcp.tool()
    def fl_restore_plugin_parameter_state(state: dict) -> dict:
        """Restore named public parameters after identity/name checks and later readback; skip unnamed wrapper placeholders; not transactional."""
        return PluginParameters().restore(state)

    # The partial parameter-only fl_clone_mixer_effect tool was removed: it was
    # superseded by fl_clone_mixer_effect_full (complete event-213 state) and
    # the near-identical name made MCP clients pick the wrong one.
