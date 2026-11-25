"""Plugin-agnostic discovery and edits via the host's actual parameter metadata."""

import hashlib
import json
import math
import re
import time
import unicodedata

from fl_studio_mcp.utils.file_rpc import FileRPC

_NAME_CACHE = {}
_VALUE_TOLERANCE = 1e-5


class EditorError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def normalized_name(name):
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def parse_target(target):
    if isinstance(target, str):
        channel = re.fullmatch(r"channel:(0|[1-9][0-9]*)", target)
        effect = re.fullmatch(r"mixer:(0|[1-9][0-9]*):([0-9])", target)
        if channel:
            return int(channel[1]), -1
        if effect:
            return int(effect[1]), int(effect[2])
    raise EditorError("InvalidTarget", "Use channel:<global index> or mixer:<track>:<slot 0..9>")


def finite_value(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def normalized_value(value):
    return finite_value(value) and 0 <= value <= 1


def value_matches(observed, expected):
    return normalized_value(observed) and math.isclose(observed, expected, abs_tol=_VALUE_TOLERANCE)


class PluginEditor:
    def __init__(self, rpc=None):
        self.rpc = rpc or FileRPC()

    def send(self, action, params):
        response = self.rpc.send(action, params)
        if not response.get("success"):
            raise EditorError(
                response.get("error_type", "NativeCallFailed"),
                response.get("error", "Host call failed"),
                native_response=response,
            )
        return response

    def call(self, module, method, args=None):
        return self.send("native.call", {"module": module, "method": method, "args": args or []})[
            "result"
        ]

    def batch(self, operations):
        if not operations:
            return []
        return [
            item["result"]
            for item in self.send("native.batch", {"operations": operations})["results"]
        ]

    def descriptor(self, target):
        index, slot = parse_target(target)
        if not self.call("plugins", "isValid", [index, slot, True]):
            raise EditorError("PluginUnavailable", "No plugin at this location", target=target)
        name, count = self.batch(
            [
                {
                    "module": "plugins",
                    "method": "getPluginName",
                    "args": [index, slot, False, True],
                },
                {"module": "plugins", "method": "getParamCount", "args": [index, slot, True]},
            ]
        )
        if not isinstance(name, str) or type(count) is not int or not 0 <= count <= 65536:
            raise EditorError("InvalidMetadata", "Host returned unexpected plugin metadata")
        heartbeat = self.rpc.heartbeat()
        if not heartbeat.get("healthy"):
            raise EditorError("ControllerUnavailable", "The controller heartbeat is stale")
        identity = {
            "target": target,
            "index": index,
            "slot": slot,
            "plugin_name": name,
            "parameter_count": count,
            "host_pid": heartbeat.get("pid"),
            "host_generation": heartbeat.get("generation"),
        }
        identity["fingerprint"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        return identity

    def names(self, descriptor, refresh=False):
        key = descriptor["fingerprint"]
        cached = _NAME_CACHE.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        result = []
        for start in range(0, descriptor["parameter_count"], 128):
            result.extend(
                self.batch(
                    [
                        {
                            "module": "plugins",
                            "method": "getParamName",
                            "args": [i, descriptor["index"], descriptor["slot"], True],
                        }
                        for i in range(start, min(start + 128, descriptor["parameter_count"]))
                    ]
                )
            )
        if self.descriptor(descriptor["target"])["fingerprint"] != key:
            raise EditorError(
                "PluginChanged", "Plugin changed while reading its parameter metadata"
            )
        if len(_NAME_CACHE) >= 128:
            _NAME_CACHE.pop(next(iter(_NAME_CACHE)))
        _NAME_CACHE[key] = (time.monotonic(), result)
        return result

    def instances(self, query=""):
        channels = self.call("channels", "channelCount", [True])
        mixer = self.call("mixer", "trackCount")
        locations = [(f"channel:{i}", i, -1) for i in range(channels)]
        locations += [(f"mixer:{i}:{slot}", i, slot) for i in range(mixer) for slot in range(10)]
        valid = []
        for start in range(0, len(locations), 128):
            page = locations[start : start + 128]
            values = self.batch(
                [
                    {"module": "plugins", "method": "isValid", "args": [i, slot, True]}
                    for _, i, slot in page
                ]
            )
            valid.extend(location for location, present in zip(page, values) if present)
        names = []
        for start in range(0, len(valid), 128):
            names.extend(
                self.batch(
                    [
                        {
                            "module": "plugins",
                            "method": "getPluginName",
                            "args": [i, slot, False, True],
                        }
                        for _, i, slot in valid[start : start + 128]
                    ]
                )
            )
        result = [
            {"target": target, "plugin_name": name, "kind": "instrument" if slot < 0 else "effect"}
            for (target, _, slot), name in zip(valid, names)
            if normalized_name(query) in normalized_name(name)
        ]
        return {
            "success": True,
            "instances": result,
            "count": len(result),
            "channel_index_space": "global",
            "coverage": "Currently loaded host-visible plugins, not the installed plugin catalog",
        }

    def describe(
        self,
        target,
        query="",
        start=0,
        limit=64,
        named_only=True,
        include_text=False,
        refresh=False,
    ):
        if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 128:
            raise EditorError("InvalidPage", "start must be nonnegative and limit must be 1..128")
        descriptor = self.descriptor(target)
        names = self.names(descriptor, refresh)
        matches = [
            i
            for i, name in enumerate(names)
            if (name or not named_only) and normalized_name(query) in normalized_name(name)
        ]
        indices = matches[start : start + limit]
        values = self.batch(
            [
                {
                    "module": "plugins",
                    "method": "getParamValue",
                    "args": [i, descriptor["index"], descriptor["slot"], True],
                }
                for i in indices
            ]
        )
        texts, text_error = [None] * len(indices), None
        if include_text:
            try:
                texts = self.batch(
                    [
                        {
                            "module": "plugins",
                            "method": "getParamValueString",
                            "args": [i, descriptor["index"], descriptor["slot"], True],
                        }
                        for i in indices
                    ]
                )
            except EditorError as exc:
                text_error = str(exc)
        return {
            "success": True,
            "plugin": descriptor,
            "parameters": [
                {
                    "index": i,
                    "name": names[i],
                    "value": value,
                    "value_text": text,
                    "named": bool(names[i]),
                    "write_status": "requires_per_edit_verification",
                }
                for i, value, text in zip(indices, values, texts)
            ],
            "matching_parameters": len(matches),
            "named_parameter_count": sum(bool(name) for name in names),
            "page": {
                "start": start,
                "limit": limit,
                "next_start": start + len(indices) if start + len(indices) < len(matches) else None,
                "space": "matching parameters",
            },
            "value_text_error": text_error,
            "capabilities": {
                "public_parameters": True,
                "value_units": "normalized_0_1",
                "writable_flags": "not_exposed_by_host",
                "opaque_state": "host/plugin preset files",
                "editor_controls": "observed UI actions for host-owned editor HWNDs",
                "unexposed_controls_are_parameters": False,
            },
            "fallback": {
                "preset_tools": ["fl_drop_asset", "fl_load_effect_preset"],
                "ui_tools": ["fl_list_windows", "fl_screenshot", "fl_plugin_editor_ui"],
            },
        }

    def edit(self, target, edits, expected_fingerprint=None, dry_run=False, allow_unnamed=False):
        parse_target(target)
        if not isinstance(edits, list) or not 1 <= len(edits) <= 64:
            raise EditorError("InvalidEdits", "Expected 1..64 parameter edits")
        if not self.rpc.heartbeat().get("healthy"):
            raise EditorError("ControllerUnavailable", "The controller heartbeat is stale")
        lock_name = "plugin-" + hashlib.sha256(target.encode()).hexdigest()[:20] + ".lock"
        with self.rpc.queue_lock(lock_name):
            return self._edit_locked(target, edits, expected_fingerprint, dry_run, allow_unnamed)

    def _edit_locked(self, target, edits, expected_fingerprint, dry_run, allow_unnamed):
        descriptor = self.descriptor(target)
        if expected_fingerprint is not None and descriptor["fingerprint"] != expected_fingerprint:
            raise EditorError(
                "PluginChanged", "Observed plugin identity no longer matches", plugin=descriptor
            )
        names, resolved, seen = None, [], set()
        for edit in edits:
            if not isinstance(edit, dict) or not normalized_value(edit.get("value")):
                raise EditorError(
                    "InvalidValue", "Each edit requires a finite normalized value in 0..1"
                )
            parameter = edit.get("parameter")
            if isinstance(parameter, str):
                names = self.names(descriptor, refresh=True) if names is None else names
                candidates = [
                    i
                    for i, name in enumerate(names)
                    if name and normalized_name(name) == normalized_name(parameter)
                ]
                if len(candidates) != 1:
                    raise EditorError(
                        "AmbiguousParameter" if candidates else "ParameterNotFound",
                        "Use a unique exact parameter name or an explicit index",
                        candidates=[{"index": i, "name": names[i]} for i in candidates[:20]],
                        fallback="Describe/search the plugin or observe its editor UI",
                    )
                parameter = candidates[0]
            if (
                type(parameter) is not int
                or not 0 <= parameter < descriptor["parameter_count"]
                or parameter in seen
            ):
                raise EditorError("InvalidParameter", "Parameter index is invalid or duplicated")
            if "expected_value" in edit and not normalized_value(edit["expected_value"]):
                raise EditorError("InvalidValue", "expected_value must be normalized in 0..1")
            if "expected_name" in edit and not isinstance(edit["expected_name"], str):
                raise EditorError("InvalidParameter", "expected_name must be a string")
            seen.add(parameter)
            resolved.append({**edit, "index": parameter})
        ops = []
        for item in resolved:
            for method in ("getParamName", "getParamValue"):
                ops.append(
                    {
                        "module": "plugins",
                        "method": method,
                        "args": [item["index"], descriptor["index"], descriptor["slot"], True],
                    }
                )
        values = self.batch(ops)
        plan = []
        for offset, item in enumerate(resolved):
            name, before = values[offset * 2 : offset * 2 + 2]
            expected_names = [item["expected_name"]] if "expected_name" in item else []
            if isinstance(item["parameter"], str):
                expected_names.append(item["parameter"])
            current_name = normalized_name(name)
            if any(current_name != normalized_name(expected) for expected in expected_names):
                raise EditorError("ParameterChanged", "Parameter name changed; no writes submitted")
            if not name and not allow_unnamed:
                raise EditorError(
                    "UnnamedParameter",
                    "Unnamed host slots are not assumed to be usable controls",
                    parameter_index=item["index"],
                )
            if not normalized_value(before):
                raise EditorError(
                    "UnsupportedValue", "Host did not provide a normalized parameter value"
                )
            if "expected_value" in item and not value_matches(before, item["expected_value"]):
                raise EditorError(
                    "ValueChanged",
                    "Parameter changed before the edit; no writes submitted",
                    parameter_index=item["index"],
                    observed=before,
                )
            plan.append(
                {"index": item["index"], "name": name, "before": before, "requested": item["value"]}
            )
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "plugin": descriptor,
                "plan": plan,
                "writes_submitted": 0,
            }
        applied = []
        for item in plan:
            submitted = False
            write_response = None
            try:
                if self.descriptor(target)["fingerprint"] != descriptor["fingerprint"]:
                    raise EditorError("PluginChanged", "Plugin changed before parameter write")
                name = self.call(
                    "plugins",
                    "getParamName",
                    [item["index"], descriptor["index"], descriptor["slot"], True],
                )
                before = self.call(
                    "plugins",
                    "getParamValue",
                    [item["index"], descriptor["index"], descriptor["slot"], True],
                )
                if name != item["name"] or not value_matches(before, item["before"]):
                    raise EditorError(
                        "ValueChanged",
                        "Parameter changed after preflight; remaining writes stopped",
                    )
                if value_matches(before, item["requested"]):
                    applied.append(
                        {
                            **item,
                            "observed": before,
                            "verified_readback": True,
                            "setter_exercised": False,
                        }
                    )
                    continue
                submitted = True
                write_response = self.send(
                    "native.call",
                    {
                        "module": "plugins",
                        "method": "setParamValue",
                        "args": [
                            item["requested"],
                            item["index"],
                            descriptor["index"],
                            descriptor["slot"],
                            0,
                            True,
                        ],
                    },
                )
                for attempt in range(5):
                    observed = self.call(
                        "plugins",
                        "getParamValue",
                        [item["index"], descriptor["index"], descriptor["slot"], True],
                    )
                    if value_matches(observed, item["requested"]):
                        break
                    if attempt < 4:
                        time.sleep(0.05)
                else:
                    raise EditorError(
                        "ValueNotConfirmed",
                        "Readback differs. The control may round values or reject writes.",
                        observed=observed,
                    )
                if self.descriptor(target)["fingerprint"] != descriptor["fingerprint"]:
                    raise EditorError("PluginChanged", "Plugin changed during write verification")
                applied.append(
                    {
                        **item,
                        "observed": observed,
                        "verified_readback": True,
                        "setter_exercised": True,
                        "request_id": write_response.get("request_id"),
                    }
                )
            except EditorError as exc:
                return {
                    "success": False,
                    "error_type": exc.code,
                    "error": str(exc),
                    "details": exc.details,
                    "plugin": descriptor,
                    "applied": applied,
                    "failed_parameter": item,
                    "write_may_have_executed": submitted,
                    "write_response": write_response,
                    "transactional": False,
                    "automatic_retry": False,
                }
        if len(applied) > 1:
            try:
                self.verify_group(descriptor, applied)
            except EditorError as exc:
                return {
                    "success": False,
                    "error_type": exc.code,
                    "error": str(exc),
                    "details": exc.details,
                    "plugin": descriptor,
                    "applied": applied,
                    "verified_readback": False,
                    "transactional": False,
                }
        return {
            "success": True,
            "plugin": descriptor,
            "applied": applied,
            "verified_readback": True,
            "transactional": False,
            "value_units": "normalized_0_1",
        }

    def verify_group(self, descriptor, applied):
        values = self.batch(
            [
                {
                    "module": "plugins",
                    "method": "getParamValue",
                    "args": [item["index"], descriptor["index"], descriptor["slot"], True],
                }
                for item in applied
            ]
        )
        if self.descriptor(descriptor["target"])["fingerprint"] != descriptor["fingerprint"]:
            raise EditorError("PluginChanged", "Plugin changed during final group verification")
        mismatches = [
            {"index": item["index"], "requested": item["requested"], "observed": value}
            for item, value in zip(applied, values)
            if not value_matches(value, item["requested"])
        ]
        if mismatches:
            raise EditorError(
                "GroupValueNotConfirmed",
                "A later edit changed a previously read-back value",
                mismatches=mismatches,
            )


def result_or_error(function):
    try:
        return function()
    except EditorError as exc:
        return {"success": False, "error_type": exc.code, "error": str(exc), **exc.details}
    except TimeoutError as exc:
        return {"success": False, "error_type": "EditorBusy", "error": str(exc)}


def register_plugin_editor_tools(mcp):
    @mcp.tool()
    def fl_list_plugin_instances(query: str = "") -> dict:
        """Discover loaded Channel Rack instruments and Mixer effects.

        Returns channel:N or mixer:N:slot targets, optionally filtered by plugin name.
        """
        return result_or_error(lambda: PluginEditor().instances(query))

    @mcp.tool()
    def fl_describe_plugin(
        target: str,
        query: str = "",
        start: int = 0,
        limit: int = 64,
        named_only: bool = True,
        include_text: bool = False,
        refresh: bool = False,
    ) -> dict:
        """Search a loaded plugin's parameter names, indices, and normalized values.

        include_text adds the plugin's display values when available. Editability is
        verified at write time.
        """
        return result_or_error(
            lambda: PluginEditor().describe(
                target, query, start, limit, named_only, include_text, refresh
            )
        )

    @mcp.tool()
    def fl_edit_plugin(
        target: str,
        edits: list[dict],
        expected_fingerprint: str | None = None,
        dry_run: bool = False,
        allow_unnamed: bool = False,
    ) -> dict:
        """Edit host-exposed parameters on an instrument or effect. Each edit contains
        parameter (exact unique name or index), value (normalized 0..1), and optional
        expected_name and expected_value. Pass the describe fingerprint to guard metadata
        and dry_run to preview. Results include independent readback and partial progress.
        """
        return result_or_error(
            lambda: PluginEditor().edit(target, edits, expected_fingerprint, dry_run, allow_unnamed)
        )

    @mcp.tool()
    def fl_plugin_editor_ui(hwnd: int, expected_title: str, operation: str, params: dict) -> dict:
        """Send input to a visible plugin editor identified by its Windows handle and title.

        Observe screenshots before and after to verify the private control changed.
        """
        from fl_studio_mcp.utils import desktop

        gui, _, _ = desktop.require_window(hwnd)
        if (
            gui.GetClassName(hwnd) not in ("TPluginForm", "TSynthEditorForm")
            or gui.GetWindowText(hwnd) != expected_title
        ):
            return {
                "success": False,
                "error_type": "EditorChanged",
                "error": "Refresh and identify the intended plugin editor HWND/title",
            }
        if operation in ("keys", "text") and gui.GetForegroundWindow() != hwnd:
            return {
                "success": False,
                "error_type": "EditorNotForeground",
                "error": "Use the observed editor window as the foreground for keyboard input",
            }
        return {
            **desktop.input_action(hwnd, operation, **params),
            "backend": "observed_ui",
            "verified_parameter_change": False,
            "requires_visual_verification": True,
        }
