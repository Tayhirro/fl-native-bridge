"""FL controller extension using public native entry points, not raw addresses."""

from importlib import import_module
import inspect
import math


MODULES = (
    "ui",
    "general",
    "mixer",
    "patterns",
    "channels",
    "plugins",
    "playlist",
    "arrangement",
    "transport",
    "device",
    "midi",
    "screen",
    "utils",
    "launchMap",
)
ACTIONS = (
    "native.capabilities",
    "native.uiSnapshot",
    "native.window",
    "native.scroll",
    "native.mixerSnapshot",
    "native.setSendLevel",
    "native.setEffectMix",
    "native.clonePattern",
    "native.renamePattern",
    "native.call",
    "native.describe",
    "native.constants",
    "native.snapshot",
    "native.batch",
)
BLOCKED_METHODS = {
    (
        "general",
        "processRECEvent",
    ): "Disabled after an FL 26.1.7.5654 engine access violation during effect parameter testing"
}


# Transport controls move the playhead / toggle recording; they do not edit
# project data and must work while FL reports safeToEdit=false during a
# recording (stopping a recording is itself a transport control).
TRANSPORT_CONTROLS = {
    "stop", "start", "record", "setSongPos", "setPlaybackSpeed",
    "setLoopMode", "globalTransport", "rewind", "fastForward",
    "armTrack", "isTrackArmed", "getTrackRecordingFileName",
}


def is_read(name):
    return name in TRANSPORT_CONTROLS or name.startswith(("get", "is")) or name in (
        "safeToEdit",
        "trackCount",
        "trackNumber",
        "channelCount",
        "channelNumber",
        "selectedChannel",
        "patternCount",
        "patternNumber",
        "patternMax",
        "currentTime",
        "currentTimeHint",
        "selectionStart",
        "selectionEnd",
        "selectionIsActive",
        "liveSelectionStart",
    )


def clean(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"nonfinite": str(value)}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, bytes):
        import base64

        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    return {"python_type": type(value).__name__, "representation": repr(value)}


def decode(value, module, depth=0):
    if depth > 16:
        raise ValueError("Native argument nesting exceeds 16 levels")
    if isinstance(value, list):
        return [decode(item, module, depth + 1) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            import base64

            return base64.b64decode(value["$bytes"], validate=True)
        if set(value) == {"$tuple"}:
            return tuple(decode(value["$tuple"], module, depth + 1))
        if set(value) == {"$constant"}:
            name, separator, field = str(value["$constant"]).partition(".")
            if not separator or name not in MODULES or field.startswith("_"):
                raise ValueError("Invalid FL constant")
            constant = getattr(module(name), field)
            if not isinstance(constant, (int, float, bool, str)):
                raise ValueError("Expected a scalar FL constant")
            return constant
        return {key: decode(item, module, depth + 1) for key, item in value.items()}
    return value


def call_api(params, module):
    name, method = params.get("module"), params.get("method")
    if name not in MODULES or not isinstance(method, str) or method.startswith("_"):
        raise ValueError("Expected a public method in an advertised FL module")
    if (name, method) in BLOCKED_METHODS:
        raise RuntimeError(BLOCKED_METHODS[(name, method)])
    target = getattr(module(name), method, None)
    if not callable(target):
        raise ValueError("Native method is unavailable: " + name + "." + method)
    args, kwargs = params.get("args", []), params.get("kwargs", {})
    if not isinstance(args, list) or not isinstance(kwargs, dict) or len(args) > 32:
        raise ValueError("args must be a list (max 32); kwargs must be an object")
    if not is_read(method):
        _editable(module("general"))
    return {
        "module": name,
        "method": method,
        "read_only": is_read(method),
        "result": clean(target(*decode(args, module), **decode(kwargs, module))),
    }


def describe(module):
    result = {}
    for name in MODULES:
        try:
            mod = module(name)
            methods = {}
            for method in dir(mod):
                function = getattr(mod, method)
                if method.startswith("_") or not callable(function):
                    continue
                try:
                    signature = str(inspect.signature(function))
                except (TypeError, ValueError):
                    signature = None
                methods[method] = dict(
                    signature=signature,
                    doc=getattr(function, "__doc__", None),
                    read_only=is_read(method),
                    enabled=(name, method) not in BLOCKED_METHODS,
                    blocked_reason=BLOCKED_METHODS.get((name, method)),
                )
            result[name] = {"available": True, "methods": methods}
        except ImportError as exc:
            result[name] = {"available": False, "error": str(exc)}
    return {"modules": result, "actions": list(ACTIONS)}


def _integer(params, key, minimum, maximum):
    value = params.get(key)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer in {minimum}..{maximum}")
    return value


def _level(params):
    value = params.get("level")
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("level must be a finite number in 0..1")
    return float(value)


def _require(module, *names):
    for name in names:
        if not callable(getattr(module, name, None)):
            raise RuntimeError(f"Unsupported API: {module.__name__}.{name}")


def _editable(general):
    _require(general, "safeToEdit")
    if not general.safeToEdit():
        raise RuntimeError("FL reports safeToEdit=false; operation was not executed")


def dispatch(action, params, modules=None):
    if action not in ACTIONS:
        raise ValueError("Unknown native action: " + str(action))
    """Handle an explicit action whitelist. No eval or arbitrary function calls."""
    if action not in ACTIONS:
        raise ValueError(f"Unknown native action: {action}")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    loaded = {} if modules is None else modules

    def module(name):
        if name not in loaded:
            loaded[name] = import_module(name)
        return loaded[name]

    if action in ("native.capabilities", "native.describe"):
        return describe(module)
    if action == "native.call":
        return call_api(params, module)
    if action == "native.constants":
        name = params.get("module", "midi")
        if name not in MODULES:
            raise ValueError("Unknown module")
        mod = module(name)
        return {
            "module": name,
            "constants": {
                n: clean(getattr(mod, n))
                for n in dir(mod)
                if not n.startswith("_") and isinstance(getattr(mod, n), (int, float, str, bool))
            },
        }
    if action == "native.batch":
        operations = params.get("operations")
        if not isinstance(operations, list) or not 1 <= len(operations) <= 128:
            raise ValueError("Expected 1..128 operations; batch is ordered, not transactional")
        results = []
        for index, operation in enumerate(operations):
            try:
                results.append(call_api(operation, module))
            except Exception as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "failed_index": index,
                    "completed": results,
                    "transactional": False,
                }
        return {"results": results, "transactional": False}
    if action == "native.snapshot":
        ui, general, mixer, channels, patterns, playlist, transport = [
            module(n)
            for n in ("ui", "general", "mixer", "channels", "patterns", "playlist", "transport")
        ]
        start = _integer({"start": params.get("start", 0)}, "start", 0, 10000)
        limit = _integer({"limit": params.get("limit", 32)}, "limit", 1, 128)

        def read(mod, name, *args):
            try:
                return clean(getattr(mod, name)(*args))
            except Exception as exc:
                return {"error": str(exc)}

        return {
            "version": read(ui, "getVersion"),
            "project_title": read(general, "getProjectTitle"),
            "changed": read(general, "getChangedFlag"),
            "safe_to_edit": read(general, "safeToEdit"),
            "transport": {
                n: read(transport, n)
                for n in ("isPlaying", "isRecording", "getSongPos", "getLoopMode")
            },
            "focused": {
                n: read(ui, n)
                for n in ("getFocusedFormID", "getFocusedFormCaption", "getFocusedPluginName")
            },
            "windows": [
                {
                    "id": i,
                    "visible": read(ui, "getVisible", i),
                    "focused": read(ui, "getFocused", i),
                }
                for i in range(5)
            ],
            "mixer_count": mixer.trackCount(),
            "channel_count": channels.channelCount(True),
            "playlist_count": playlist.trackCount(),
            "pattern_count": patterns.patternCount(),
            "current_pattern": read(patterns, "patternNumber"),
            "selected_channel": read(channels, "selectedChannel", False, 0, True),
            "channel_index_space": "global",
            "mixer": [
                {
                    "index": i,
                    "name": read(mixer, "getTrackName", i),
                    "volume": read(mixer, "getTrackVolume", i),
                    "pan": read(mixer, "getTrackPan", i),
                    "muted": read(mixer, "isTrackMuted", i),
                    "solo": read(mixer, "isTrackSolo", i),
                }
                for i in range(start, min(start + limit, mixer.trackCount()))
            ],
            "channels": [
                {
                    "index": i,
                    "name": read(channels, "getChannelName", i, True),
                    "mixer": read(channels, "getTargetFxTrack", i, True),
                    "volume": read(channels, "getChannelVolume", i, False, True),
                }
                for i in range(start, min(start + limit, channels.channelCount(True)))
            ],
            "playlist": [
                {
                    "index": i,
                    "name": read(playlist, "getTrackName", i),
                    "muted": read(playlist, "isTrackMuted", i),
                }
                for i in range(start + 1, min(start + limit, playlist.trackCount()) + 1)
            ],
            "page": {"start": start, "limit": limit},
            "coverage": "Public getters plus desktop state; clips and opaque plugin internals require separate observation",
        }

    if action == "native.uiSnapshot":
        ui, general = module("ui"), module("general")
        required = (
            "getVersion",
            "getProgTitle",
            "getFocusedFormID",
            "getFocusedFormCaption",
            "getFocusedPluginName",
            "getVisible",
            "getFocused",
        )
        _require(ui, *required)
        _require(general, "safeToEdit", "getChangedFlag", "getProjectTitle")
        return {
            "version": ui.getVersion(),
            "program_title": ui.getProgTitle(),
            "project_title": general.getProjectTitle(),
            "changed": general.getChangedFlag(),
            "safe_to_edit": bool(general.safeToEdit()),
            "focused_form_id": ui.getFocusedFormID(),
            "focused_caption": ui.getFocusedFormCaption(),
            "focused_plugin": ui.getFocusedPluginName(),
            "windows": [
                {"id": i, "visible": bool(ui.getVisible(i)), "focused": bool(ui.getFocused(i))}
                for i in range(5)
            ],
            "coverage": "Main form visibility/focus; no HWND geometry or clip contents",
        }

    if action in ("native.window", "native.scroll"):
        ui, general = module("ui"), module("general")
        index = _integer(params, "window", 0, 4 if action == "native.window" else 2)
        if action == "native.window":
            names = {"show": "showWindow", "hide": "hideWindow", "focus": "setFocused"}
            name = names.get(params.get("operation"))
            if name is None:
                raise ValueError("operation must be show, hide or focus")
            _require(ui, name)
            _editable(general)
            getattr(ui, name)(index)
            return {"window": index, "operation": params["operation"], "executed": True}
        value = _integer(params, "value", 0, 2147483647)
        direction = _integer(params, "direction", 0, 1)
        _require(ui, "scrollWindow")
        _editable(general)
        ui.scrollWindow(index, value, direction)
        return {"window": index, "value": value, "direction": direction, "executed": True}

    if action in ("native.clonePattern", "native.renamePattern"):
        patterns, general = module("patterns"), module("general")
        _require(patterns, "patternMax")
        index = _integer(params, "index", 1, patterns.patternMax())
        if action == "native.clonePattern":
            _require(patterns, "clonePattern", "patternCount")
            _editable(general)
            before = patterns.patternCount()
            patterns.clonePattern(index)
            return {
                "source": index,
                "count_before": before,
                "count_after": patterns.patternCount(),
                "executed": True,
                "verified_persistence": False,
            }
        name = params.get("name")
        if not isinstance(name, str) or not name.strip() or "\0" in name or len(name) > 256:
            raise ValueError("name must contain 1..256 characters and no NUL")
        _require(patterns, "setPatternName", "getPatternName")
        _editable(general)
        patterns.setPatternName(index, name)
        actual = patterns.getPatternName(index)
        if actual != name:
            raise RuntimeError(f"Pattern name readback differs: {actual!r}")
        return {"index": index, "name": actual, "verified_readback": True}

    mixer = module("mixer")
    _require(mixer, "trackCount")
    maximum = mixer.trackCount() - 1
    if action == "native.mixerSnapshot":
        index = _integer(params, "index", 0, maximum)
        _require(
            mixer,
            "getTrackName",
            "getTrackVolume",
            "getTrackPan",
            "getTrackPeaks",
            "getRouteSendActive",
            "getRouteToLevel",
        )
        return {
            "index": index,
            "name": mixer.getTrackName(index),
            "volume": mixer.getTrackVolume(index),
            "pan": mixer.getTrackPan(index),
            "peaks": [mixer.getTrackPeaks(index, side) for side in (0, 1)],
            "routes": [
                {"destination": dest, "level": mixer.getRouteToLevel(index, dest)}
                for dest in range(maximum + 1)
                if dest != index and mixer.getRouteSendActive(index, dest)
            ],
            "coverage": "One point-in-time sample; peaks are not loudness measurements",
        }

    if action == "native.setSendLevel":
        source = _integer(params, "source", 1, maximum)
        dest = _integer(params, "destination", 0, maximum)
        if source == dest:
            raise ValueError("Self routing is not supported")
        level = _level(params)
        _require(mixer, "getRouteSendActive", "setRouteToLevel", "getRouteToLevel")
        if not mixer.getRouteSendActive(source, dest):
            raise ValueError(
                "Route must already exist; this command does not create a routing edge"
            )
        _editable(module("general"))
        mixer.setRouteToLevel(source, dest, level)
        actual = mixer.getRouteToLevel(source, dest)
        return {
            "source": source,
            "destination": dest,
            "requested_level": level,
            "observed_level": actual,
            "verified_readback": math.isclose(level, actual, abs_tol=1e-5),
        }

    index = _integer(params, "index", 0, maximum)
    slot = _integer(params, "slot", 0, 9)
    level = _level(params)
    plugins = module("plugins")
    _require(plugins, "isValid")
    if not plugins.isValid(index, slot):
        raise ValueError("The requested mixer effect slot is empty or invalid")
    _require(mixer, "setPluginMixLevel", "getPluginMixLevel")
    _editable(module("general"))
    mixer.setPluginMixLevel(index, slot, level)
    actual = mixer.getPluginMixLevel(index, slot)
    return {
        "index": index,
        "slot": slot,
        "requested_level": level,
        "observed_level": actual,
        "verified_readback": math.isclose(level, actual, abs_tol=1e-5),
    }
