"""MCP tools backed by the typed in-process native operation bridge."""

from __future__ import annotations

import struct
import time
import wave
from pathlib import Path
from uuid import uuid4

from fl_studio_mcp.utils import write_guard as guard
from fl_studio_mcp.utils.connection import get_connection
from fl_studio_mcp.utils.flp_events import (
    flp_event_layout as _flp_event_layout,
)
from fl_studio_mcp.utils.flp_events import (
    flp_read_varint as _flp_read_varint,
)
from fl_studio_mcp.utils.flp_events import flp_varint  # noqa: F401
from fl_studio_mcp.utils.flp_events import (
    fst_with_plugin_state as _fst_with_plugin_state,
)

_flp_varint = flp_varint  # re-exported for existing callers


def parse_flp_sampler_sources(flp_bytes: bytes) -> list[dict]:
    """Read Sampler source paths from a saved .flp without pyflp.

    pyflp 0.x parses FLP 12.5 event streams by decoding every text event as
    UTF-16; on this build a Sampler's binary parameter event triggers an
    illegal-surrogate error and aborts the whole parse. This tolerant scanner
    walks the event stream by its public sizes and only decodes the events we
    need: ChannelID.New (WORD = 64) starts a channel, ChannelID.SamplePath
    (TEXT + 4 = 196) is a UTF-16LE string holding that channel's sample file.
    Channel order and the embedded channel iid are preserved, so a path can be
    matched to the global Channel Rack index reported by the controller.

    Returns one entry per channel that carries a SamplePath event:
    {channel_index, channel_iid, sample_path}. Unknown events are skipped by
    size, never decoded. A malformed stream raises ValueError.
    """
    if len(flp_bytes) < 22 or flp_bytes[:4] != b"FLhd" or flp_bytes[14:18] != b"FLdt":
        raise ValueError("Not a complete FL Studio project (FLhd/FLdt header)")
    declared = int.from_bytes(flp_bytes[18:22], "little")
    if declared != len(flp_bytes) - 22:
        raise ValueError("FLP data chunk size does not match the file")
    pos, end = 22, len(flp_bytes)
    channels: list[dict] = []
    current: dict | None = None
    index = -1
    while pos < end:
        event_id = flp_bytes[pos]
        pos += 1
        if event_id < 64:             # BYTE events: 1-byte payload
            size = 1
        elif event_id < 128:          # WORD events: 2-byte payload
            size = 2
        elif event_id == 0xAC:        # tagged event: tag byte + u16/u32 payload
            if pos >= end:
                raise ValueError("FLP tagged event is truncated")
            tag = flp_bytes[pos]
            size = 1 + (2 if tag == 1 else 4)
        elif event_id < 192:          # DWORD events: 4-byte payload
            size = 4
        else:                         # TEXT/DATA events: varint length + data
            size, pos = _flp_read_varint(flp_bytes, pos)
        if pos + size > end:
            raise ValueError("FLP event extends past end of file")
        payload = flp_bytes[pos:pos + size]
        pos += size
        if event_id == 64:            # ChannelID.New (WORD)
            index += 1
            current = {"channel_index": index,
                       "channel_iid": int.from_bytes(payload[:2], "little")
                       if len(payload) >= 2 else index,
                       "sample_path": None}
            channels.append(current)
        elif current is not None and event_id == 196:  # ChannelID.SamplePath
            current["sample_path"] = payload.decode("utf-16-le", "strict").rstrip("\x00")
    return [c for c in channels if c["sample_path"] is not None]

def parse_flp_plugin_states(flp_bytes: bytes) -> dict:
    """Read plugin-data records (FLP event 213) and attribute them to owners.

    Does not use pyflp: this build's event stream contains a tagged 0xAC event
    that desynchronises naive walkers. Two framings are tried in order — the
    tagged rule (event 0xAC = tag byte + u16/u32 payload) used by FL 26.1.3+,
    then the legacy fixed-size rule — and only a walk that lands exactly on the
    FLdt chunk end is accepted. Attribution mirrors the reference reader:
    event 64 starts a channel section, event 236 starts a Mixer insert section
    (Master first, then physical order), event 213 is that owner's plugin-data
    record, and event 98 (u16 slot index) closes a Mixer slot (empty slots emit
    only the 98 marker). Nothing else is decoded.

    Returns {"framing": ..., "version": ..., "channels": {index: bytes},
             "mixer_slots": {(insert, slot): bytes}}.
    """
    if len(flp_bytes) < 22 or flp_bytes[:4] != b"FLhd":
        raise ValueError("Not an FL Studio project (missing FLhd)")
    header_length = int.from_bytes(flp_bytes[4:8], "little")
    chunk = 8 + header_length
    if chunk + 8 > len(flp_bytes) or flp_bytes[chunk:chunk + 4] != b"FLdt":
        raise ValueError("FL project image has no FLdt data chunk")
    data_length = int.from_bytes(flp_bytes[chunk + 4:chunk + 8], "little")
    start, end = chunk + 8, chunk + 8 + data_length
    if end > len(flp_bytes):
        raise ValueError("FL project image is truncated")

    def read_payload(pos: int, event_id: int, framing: str) -> tuple[bytes, int]:
        if event_id == 0xAC and framing == "tagged":
            if pos >= end:
                raise ValueError("event 0xAC has no tag byte")
            size = 1 + (2 if flp_bytes[pos] == 1 else 4)
        elif event_id < 64:
            size = 1
        elif event_id < 128:
            size = 2
        elif event_id < 192:
            size = 4
        else:
            value = 0
            shift = 0
            while True:
                if pos >= end:
                    raise ValueError("truncated varint length")
                byte = flp_bytes[pos]
                pos += 1
                value |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
                if shift > 35:
                    raise ValueError("malformed varint length")
            size = value
        if pos + size > end:
            raise ValueError("event payload extends past FLdt chunk end")
        return flp_bytes[pos:pos + size], pos + size

    def walk(framing: str) -> dict:
        pos = start
        channels: dict[int, bytes] = {}
        slots: dict[tuple[int, int], bytes] = {}
        version = ""
        channel_index = -1
        insert_index = -1
        pending: bytes | None = None
        while pos < end:
            event_id = flp_bytes[pos]
            pos += 1
            payload, pos = read_payload(pos, event_id, framing)
            if event_id == 64:
                channel_index = int.from_bytes(payload[:2], "little")
            elif event_id == 236:
                insert_index += 1
                pending = None
            elif event_id == 98 and insert_index >= 0:
                if pending is not None:
                    slots[(insert_index, int.from_bytes(payload[:2], "little"))] = pending
                pending = None
            elif event_id == 213:
                if insert_index >= 0:
                    pending = payload
                elif channel_index >= 0:
                    channels[channel_index] = payload
            elif event_id == 199 and not version:
                version = payload.decode("ascii", "replace").rstrip("\x00")
        if pos != end:
            raise ValueError("event walk did not land on chunk end")
        return {"framing": framing, "version": version,
                "channels": channels, "mixer_slots": slots}

    first_error = None
    for framing in ("tagged", "legacy"):
        try:
            return walk(framing)
        except ValueError as exc:
            first_error = first_error or exc
    raise ValueError(f"FLP event stream unreadable: {first_error}")

def _checked_effect_preset(path: Path) -> Path:
    path = path.resolve(strict=True)
    folders = [part.casefold() for part in path.parent.parts]
    database = next((i for i in range(len(folders) - 1, -1, -1)
                     if folders[i].startswith("plugin database")), -1)
    presets = next((i for i in range(len(folders) - 1, -1, -1)
                    if folders[i].startswith("plugin presets")), -1)
    origin = max(database, presets)
    relative = folders[origin + 1 :] if origin >= 0 else []
    if (origin < 0 or (relative[:1] != ["effects"]
                       and relative[:2] != ["installed", "effects"])
            or path.suffix.casefold() != ".fst" or path.stem.casefold() == "(delete) effect"):
        raise ValueError("Use an .fst from Plugin Database Effects or Plugin presets Effects")
    with path.open("rb") as stream:
        header = stream.read(22)
    if (len(header) != 22 or header[:10] != b"FLhd\x06\0\0\0\x30\0"
            or header[14:18] != b"FLdt"
            or int.from_bytes(header[18:22], "little") != path.stat().st_size - 22):
        raise ValueError("The file is not a complete FL Studio effect preset")
    return path

_ADAPTER_CAPABILITIES = {
    "fl-native-effect": {
        "parameters": "named-public + unnamed slots skipped on restore",
        "state_read": True,
        "state_clone": "factory FST skeleton + replaced event 213",
        "preset_list": True,
        "preset_load": "empty slot direct; occupied slot remove+load (irreversible)",
        "wrapper_state_file": False,
        "notes": "FL's own effect; state is a compact event-213 record.",
    },
    "vst-wrapper": {
        "parameters": "named-public; unnamed wrapper slots skipped",
        "state_read": True,
        "state_clone": "factory FST skeleton + replaced event 213 (verify digest)",
        "preset_list": True,
        "preset_load": "empty slot direct; occupied slot remove+load (irreversible)",
        "wrapper_state_file": True,
        "notes": ("Hosted third-party plugin; the wrapper dispatcher (opcode 0x12) "
                  "can load a real state/preset file into a live instance."),
    },
    "unknown": {
        "parameters": "named-public",
        "state_read": True,
        "state_clone": "best effort",
        "preset_list": True,
        "preset_load": "empty slot direct",
        "wrapper_state_file": False,
        "notes": "State record carried no recognisable wrapper marker.",
    },
}

def classify_plugin_state(plugin_name: str, state: bytes) -> dict:
    """Classify a plugin from its event-213 state record (pure, testable).

    A VST wrapper's record embeds the plugin's file path, so the path extension
    identifies the format. FL's own effects store a compact record without one.
    """
    lowered = (state or b"").lower()
    markers = {
        "vst3": (b".vst3", ".vst3".encode("utf-16-le").lower()),
        "vst2": (b".dll", ".dll".encode("utf-16-le").lower()),
    }
    if "fruity wrapper" in (plugin_name or "").casefold():
        kind = "vst-wrapper"
    elif any(m in lowered for m in markers["vst3"]):
        kind = "vst3-wrapper"
    elif any(m in lowered for m in markers["vst2"]):
        kind = "vst2-wrapper"
    elif state:
        kind = "fl-native-effect"
    else:
        kind = "unknown"
    capabilities = dict(_ADAPTER_CAPABILITIES.get(
        "vst-wrapper" if kind.startswith("vst") else kind,
        _ADAPTER_CAPABILITIES["unknown"]))
    marker = next((m for group in markers.values() for m in group
                   if m in lowered), None)
    return {"plugin_name": plugin_name, "adapter": kind,
            "state_bytes": len(state or b""), "capabilities": capabilities,
            "has_format_marker": marker is not None}

def fl_describe_plugin_adapter(track: int, slot: int) -> dict:
    """Report which adapter handles a Mixer FX slot and what it supports.

    Reads the plugin name and its event-213 state record from a save-copy, then
    classifies it as an FL-native effect or a hosted VST wrapper. Mixer track 0
    is Master. The capability table is the registry entry used by clone/preset
    helpers; it is descriptive, not a promise that every action was exercised on
    this plugin.
    """
    import tempfile

    if type(track) is not int or type(slot) is not int or not 0 <= track <= 500 \
            or not 0 <= slot <= 9:
        return {"success": False, "error": "Invalid track or slot"}
    name_response = get_connection().send_command("native.call", {
        "request_id": uuid4().hex, "module": "plugins", "method": "getPluginName",
        "args": [track, slot, False, True],
    }, timeout=15.0)
    if not name_response.get("success") or not isinstance(name_response.get("result"), str) \
            or not name_response["result"]:
        return {"success": False, "error": "The slot is empty"}
    name = name_response["result"]
    saved = None
    try:
        handle, temp_name = tempfile.mkstemp(prefix="flmcp-adapter-", suffix=".flp")
        import os as _os
        _os.close(handle)
        saved = Path(temp_name)
        copy = fl_save_project_copy(str(saved))
        if not copy.get("success"):
            return {"success": False, "plugin_name": name,
                    "error": copy.get("error") or "Save-copy failed"}
        parsed = parse_flp_plugin_states(saved.read_bytes())
        state = parsed["mixer_slots"].get((track, slot)) or b""
        info = classify_plugin_state(name, state)
        info.update(success=True, track=track, slot=slot)
        return info
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "plugin_name": name, "error": str(exc)}
    finally:
        if saved is not None:
            saved.unlink(missing_ok=True)

def _preset_roots(engine_path: str, kind: str) -> list[tuple[str, Path]]:
    """Factory and user preset folders for a plugin family."""
    sub = "Effects" if kind == "effect" else "Generators"
    factory = Path(engine_path).parent / "Data/Patches/Plugin presets" / sub
    user = (Path.home() / "Documents/Image-Line/FL Studio/Presets"
            / "Plugin presets" / sub)
    return [("factory", factory), ("user", user)]

def fl_list_plugin_presets(plugin_name: str, kind: str = "effect") -> dict:
    """List a plugin's own preset files (.fst) by name.

    FL stores each plugin's factory presets as .fst files under
    Data/Patches/Plugin presets/<Effects|Generators>/<Plugin>/ and user presets
    under Documents/Image-Line/FL Studio/Presets/... . This scans those folders
    instead of stepping plugins.nextPreset (which cannot report the preset name).
    Names are the file stems; relative paths include sub-folders. A user preset
    with the same relative path takes precedence. kind is "effect" or "generator".
    """
    if not isinstance(plugin_name, str) or not plugin_name.strip():
        return {"success": False, "error": "plugin_name is required"}
    if kind not in ("effect", "generator"):
        return {"success": False, "error": "kind must be effect or generator"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "error": native.get("error") or "Native bridge unavailable"}
    engine_path = native.get("engine_path")
    if not isinstance(engine_path, str) or not engine_path:
        return {"success": False, "error": "The bridge did not report the engine path"}
    found: dict[str, dict] = {}
    scanned = []
    for origin, root in _preset_roots(engine_path, kind):
        folder = root / plugin_name
        if not folder.is_dir():
            scanned.append({"origin": origin, "folder": str(folder), "exists": False})
            continue
        scanned.append({"origin": origin, "folder": str(folder), "exists": True})
        for item in folder.rglob("*.fst"):
            relative = str(item.relative_to(folder))
            key = relative.casefold()
            entry = {"name": item.stem, "relative": relative,
                     "path": str(item), "origin": origin,
                     "bytes": item.stat().st_size}
            if key in found and origin == "factory" and found[key]["origin"] == "user":
                continue
            found[key] = entry
    presets = sorted(found.values(), key=lambda e: e["relative"].casefold())
    return {"success": True, "plugin_name": plugin_name, "kind": kind,
            "count": len(presets), "presets": presets, "scanned": scanned,
            "note": "Names come from preset files; FL exposes no API for the current preset name."}

def fl_load_plugin_preset(
    plugin_name: str, preset: str, track: int, slot: int, dry_run: bool = False,
) -> dict:
    """Load one named preset into a Mixer effect slot.

    preset accepts an absolute .fst path, a path relative to the plugin's preset
    folder, or a bare file stem (which must match exactly one preset). An empty
    slot is loaded directly. An occupied slot must already host the same plugin:
    the occupant is removed and the preset loaded in its place, so the previous
    unsaved settings are discarded and the removal has no restorable FL undo.
    dry_run resolves the preset and reports the plan without writing.
    """
    import time

    if (not isinstance(plugin_name, str) or not plugin_name.strip()
            or not isinstance(preset, str) or not preset.strip()
            or type(track) is not int or not 0 <= track <= 500
            or type(slot) is not int or not 0 <= slot <= 9
            or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid request"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or "Native bridge unavailable"}
    engine_path = native.get("engine_path")
    context = capabilities["context"]
    if not isinstance(engine_path, str) or not engine_path:
        return {"success": False, "submitted": False,
                "error": "The bridge did not report the engine path"}
    plugin_name = plugin_name.strip()
    # Resolve the preset file.
    target = None
    try:
        candidate = Path(preset)
        if candidate.is_absolute():
            target = _checked_effect_preset(candidate)
        else:
            matches = []
            for _origin, root in _preset_roots(engine_path, "effect"):
                folder = root / plugin_name
                if not folder.is_dir():
                    continue
                direct = folder / preset
                if direct.is_file():
                    matches.append(direct)
                else:
                    matches.extend(p for p in folder.rglob(preset)
                                   if p.suffix.casefold() == ".fst")
                    matches.extend(p for p in folder.rglob("*.fst")
                                   if p.stem.casefold() == preset.casefold())
            unique = sorted({str(m.resolve()) for m in matches})
            if len(unique) > 1:
                return {"success": False, "submitted": False,
                        "error": "Preset name is ambiguous; pass a relative path",
                        "matches": unique}
            if not unique:
                return {"success": False, "submitted": False,
                        "error": "No matching preset for %r" % preset}
            target = _checked_effect_preset(Path(unique[0]))
    except (OSError, ValueError) as exc:
        return {"success": False, "submitted": False, "error": str(exc)}
    plan = {"success": True, "submitted": False, "dry_run": dry_run,
            "plugin_name": plugin_name, "preset": str(target),
            "preset_name": target.stem, "preset_bytes": target.stat().st_size,
            "track": track, "slot": slot}
    if dry_run:
        return plan
    occupant = get_connection().send_command("native.call", {
        "request_id": uuid4().hex, "module": "plugins", "method": "getPluginName",
        "args": [track, slot, False, True],
    }, timeout=15.0)
    occupied = bool(occupant.get("success") and isinstance(occupant.get("result"), str)
                    and occupant.get("result"))
    if occupied and occupant["result"].casefold() != plugin_name.casefold():
        return {**plan, "success": False,
                "error": "Slot hosts %r, not %r" % (occupant["result"], plugin_name)}
    if occupied:
        removed = get_connection().send_command("native.removeMixerEffect", {
            "request_id": uuid4().hex, "context": context,
            "track": track, "slot": slot, "dry_run": False,
        }, timeout=40.0)
        if not removed.get("success"):
            return {**plan, "success": False, "submitted": False,
                    "error": removed.get("error") or "Removing the occupant failed"}
        time.sleep(0.15)

    class _Conn:
        def send_command(self, action, params=None, timeout=10):
            return get_connection().send_command(action, params or {}, timeout=timeout)

    loaded = load_effect_at_slot(_Conn(), plugin_name, track, slot, False,
                                 preset_path=str(target))
    plan.update(submitted=True, success=bool(loaded.get("verified")),
                replaced=occupied,
                irreversible=occupied,
                error=None if loaded.get("verified") else
                (loaded.get("error") or "Preset did not load"))
    if occupied:
        plan["note"] = ("Occupant removed and replaced; removal has no restorable "
                        "undo on this build")
    return plan

def _checked_generator_preset(path: Path) -> Path:
    path = path.resolve(strict=True)
    folders = [part.casefold() for part in path.parent.parts]
    # FL ships the factory database as "Plugin database (alphabetical)". Match the
    # prefix, taking the DEEPEST hit: a shallower "Plugin databases" (plural)
    # folder would otherwise win and hide the real database folder.
    database = next((i for i in range(len(folders) - 1, -1, -1)
                     if folders[i].startswith("plugin database")), -1)
    relative = folders[database + 1 :]
    if (database < 0 or (relative[:1] != ["generators"]
                         and relative[:2] != ["installed", "generators"])
            or path.suffix.casefold() != ".fst"):
        raise ValueError("Use a .fst from Plugin Database Generators or Installed/Generators")
    with path.open("rb") as stream:
        header = stream.read(22)
    if (len(header) != 22 or header[:10] != b"FLhd\x06\0\0\0\x30\0"
            or header[14:18] != b"FLdt"
            or int.from_bytes(header[18:22], "little") != path.stat().st_size - 22):
        raise ValueError("The file is not a complete FL Studio generator preset")
    return path

def _generator_preset(name: str, engine_path: str, preset_path: str | None) -> Path:
    if preset_path is not None:
        if not Path(preset_path).is_absolute():
            raise ValueError("The preset path must be absolute")
        return _checked_generator_preset(Path(preset_path))
    user_db = Path.home() / "Documents/Image-Line/FL Studio/Presets/Plugin database"
    factory_db = (Path(engine_path).parent / "Data/System/Plugin databases"
                  / "Plugin database (alphabetical)")
    for folder in (user_db / "Generators", factory_db / "Generators",
                   factory_db / "Installed/Generators"):
        matches = [p for p in folder.rglob("*.fst") if p.stem.casefold() == name.casefold()]
        if len(matches) > 1:
            raise ValueError("Multiple generator presets match. Select one with preset_path.")
        if matches:
            return _checked_generator_preset(matches[0])
    raise ValueError("No matching Generator preset was found in the Plugin Database")

def _effect_preset(name: str, engine_path: str, preset_path: str | None) -> Path:
    if preset_path is not None:
        if not Path(preset_path).is_absolute():
            raise ValueError("The preset path must be absolute")
        return _checked_effect_preset(Path(preset_path))
    user_db = Path.home() / "Documents/Image-Line/FL Studio/Presets/Plugin database"
    factory_db = Path(engine_path).parent / "Data/Patches/Plugin database"
    for folder in (user_db / "Effects", factory_db / "Effects", user_db / "Installed/Effects"):
        matches = [p for p in folder.rglob("*.fst") if p.stem.casefold() == name.casefold()]
        if len(matches) > 1:
            raise ValueError("Multiple effect presets match. Select one with preset_path.")
        if matches:
            return _checked_effect_preset(matches[0])
    raise ValueError("No matching Mixer effect preset was found in the Plugin Database")

def load_effect_at_slot(
    conn, name: str, track: int, slot: int, dry_run: bool, preset_path: str | None = None,
    context: str | None = None, engine_path: str | None = None,
    lease_token: str | None = None,
) -> dict:
    """Load a preset into an exact slot.

    A compound caller can pin ``context`` and ``engine_path`` captured before
    its first write; when given, they are re-checked against the live loader
    instead of silently replaced by a fresh context mid-transaction.
    ``preset_path`` may be a prepared synthetic FST, so callers can stage every
    file before the first mutation.
    """
    target = {"track": track, "slot": slot}
    if type(track) is not int or track < 0 or type(slot) is not int or not 0 <= slot < 10:
        return {**target, "success": False, "submitted": False, "error": "Invalid track or slot"}
    if not isinstance(name, str) or not name.strip():
        return {**target, "success": False, "submitted": False, "error": "Effect name is required"}
    name = name.strip()
    try:
        host = conn.send_command("mixer.effectLoaderInfo", {}, timeout=5.0)
    except RuntimeError as exc:
        return {**target, "success": False, "submitted": False, "error": str(exc)}
    live_context = host.get("context")
    live_engine = host.get("engine_path")
    if not host.get("success") or not isinstance(live_context, str) or not isinstance(live_engine, str):
        return {
            **target, "success": False, "submitted": False,
            "error": host.get("error") or "The local effect loader is unavailable",
        }
    if context is not None and live_context != context:
        return {**target, "success": False, "submitted": False,
                "error": "The FL controller or current project changed before insertion",
                "context": live_context, "expected_context": context}
    if engine_path is not None and live_engine != engine_path:
        return {**target, "success": False, "submitted": False,
                "error": "The FL engine changed before insertion",
                "engine_path": live_engine, "expected_engine_path": engine_path}
    try:
        preset = _effect_preset(name, live_engine, preset_path)
    except (OSError, ValueError) as exc:
        return {**target, "success": False, "submitted": False, "error": str(exc)}
    request_id = uuid4().hex
    result = conn.send_command(
        "mixer.loadEffectAtSlot",
        {**target, "name": name, "dry_run": dry_run, "request_id": request_id,
         "context": context, "preset_path": str(preset), "deadline": time.time() + 10,
         **({"lease_token": lease_token} if lease_token else {})},
        timeout=30.0,
    )
    if result.get("effect_request_id") != request_id:
        error = result.get("error", "Could not match FL Studio's response to this request")
        if isinstance(error, str) and "Unknown action" in error:
            return {**target, "success": False, "submitted": False,
                    "error": "Update and reload the controller script to use exact slot loading"}
        return {**target, "success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": error}
    if result.get("pending"):
        return {**target, "success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "pending": True}
    if not result.get("submitted"):
        return {**result, **target}

    read_id = uuid4().hex
    observed = conn.send_command("mixer.readEffectSlot", {**target, "request_id": read_id})
    if (observed.get("editor_request_id") != read_id or not observed.get("success")
            or observed.get("context") != context):
        return {**target, "success": False, "submitted": True, "write_outcome_unknown": True,
                "request_id": request_id, "context": context,
                "error": "The loader was called, but the requested slot could not be read back"}
    actual = observed.get("plugin_name")
    verified = (
        observed.get("track") == track
        and observed.get("slot") == slot
        and observed.get("occupied") is True
        and isinstance(actual, str)
        and actual.strip().casefold() == name.strip().casefold()
    )
    return {
        **target, "submitted": True, "plugin_name": actual,
        "success": verified and bool(result.get("success")),
        "verified": verified,
        # Both IDs must survive the wrapper: the correlation ID for the
        # controller echo and the queue envelope ID for fl_get_request_status.
        "effect_request_id": result.get("effect_request_id") or request_id,
        "queue_request_id": result.get("queue_request_id"),
        **({"error": result.get("error") or "The requested effect was not confirmed in this slot"}
           if not verified or not result.get("success") else {}),
    }

def _queue_status(request_id: str) -> dict:
    """Resolve a queue envelope ID through the same store the MCP status tool uses."""
    from fl_studio_mcp.utils.file_rpc import FileRPC

    return FileRPC().status(request_id)


def _step_outcome(response: dict) -> str:
    return guard.step_outcome(response)


def _step_record(name: str, correlation_id: str, response: dict,
                 slot_state: dict | None = None) -> dict:
    return guard.step_record(name, correlation_id, response, slot_state)


def _status_request_ids(steps: list[dict]) -> list[str]:
    return guard.status_request_ids(steps)


def _terminal(steps: list[dict]) -> bool:
    return guard.terminal(steps, _queue_status)


def _release_when_final(paths: list[Path], steps: list[dict]) -> list[str]:
    return guard.release_when_final(paths, steps, _queue_status)


def _pin(capabilities: dict) -> tuple[str, str] | None:
    return guard.pin_context(capabilities)


def fl_list_capabilities() -> dict:
    """Read the current controller session and native bridge compatibility.

    A native operation is available only after the running engine matches its
    verified build profile. This response does not describe plugin-private controls.
    """
    request_id = uuid4().hex
    try:
        response = get_connection().send_command(
            "native.semanticDescribe", {"request_id": request_id}, timeout=5.0
        )
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}
    if response.get("capability_request_id") != request_id:
        return {
            "success": False,
            "error": response.get("error") or "Controller response could not be matched",
        }
    return response

def fl_list_playlist_clips(offset: int = 0, limit: int = 32) -> dict:
    """Read a page of clips in the active arrangement without changing selection.

    Positions and lengths are PPQ ticks, and Playlist tracks are one-based.
    Snapshot references identify this observed state and must be refreshed after edits.
    This requires the Windows native bridge for the verified FL Studio build.
    """
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 64:
        return {"success": False, "error": "offset must be nonnegative and limit must be 1..64"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "error": native.get("error") or capabilities.get("error")}
    if "playlist.read_clips" not in native.get("operations", []):
        return {"success": False, "error": "Playlist clip reading is unavailable on this bridge"}

    import time

    request_id = uuid4().hex
    context = capabilities["context"]
    try:
        response = get_connection().send_command(
            "native.listPlaylistClips",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "offset": offset, "limit": limit},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "request_id": request_id, "error": str(exc)}
    if response.get("native_request_id") != request_id:
        return {"success": False, "request_id": request_id,
                "error": response.get("error") or "Controller response could not be matched"}
    return response


def fl_add_sampler(path: str, dry_run: bool = False) -> dict:
    """Append a Sampler for a WAV file and verify the loaded source path.

    This creates a Channel Rack Sampler, not a Playlist Audio Clip. After a
    submitted insertion the tool reads the new channel's sample path from a
    project save-copy and compares it with the requested file; a match reports
    state="verified" with source_path_verified=True, otherwise the insertion
    stays "partial" and the observed path is returned for inspection. Undo may
    require two FL undo steps: sample load, then channel creation.
    """
    if not isinstance(path, str) or type(dry_run) is not bool:
        return {"success": False, "submitted": False, "error": "Invalid path or dry_run"}
    try:
        source = Path(path)
        if not source.is_absolute() or source.suffix.casefold() != ".wav":
            raise ValueError("Use an absolute WAV file path")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("WAV path is not a file")
        with wave.open(str(source), "rb") as audio:
            if audio.getnchannels() < 1 or audio.getframerate() < 1 or audio.getnframes() < 1:
                raise ValueError("WAV file has no audio frames")
    except (OSError, ValueError, EOFError, wave.Error) as exc:
        return {"success": False, "submitted": False, "error": str(exc)}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if (not capabilities.get("success") or not native.get("available") or
            "channel.add_sampler" not in native.get("operations", [])):
        return {"success": False, "submitted": False,
                "error": native.get("error") or "Sampler creation is unavailable on this bridge"}
    try:
        conn = get_connection()
        count = conn.send_command("native.call", {
            "module": "channels", "method": "channelCount", "args": [True],
        })
        if not count.get("success") or type(count.get("result")) is not int:
            return {"success": False, "submitted": False,
                    "error": count.get("error") or "Cannot read the current channel count"}
        request_id = uuid4().hex
        result = conn.send_command("native.addSampler", {
            "request_id": request_id, "context": capabilities["context"],
            "expected_count": count["result"], "path": str(source), "dry_run": dry_run,
        }, timeout=30.0)
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id,
                "error": result.get("error") or "Response was not matched"}
    if result.get("submitted") and result.get("state") == "partial":
        try:
            observed_context = fl_list_capabilities()
            observed_type = conn.send_command("native.call", {
                "module": "channels", "method": "getChannelType", "args": [count["result"], True],
            })
            observed_name = conn.send_command("native.call", {
                "module": "channels", "method": "getChannelName", "args": [count["result"], True],
            })
            observed_count = conn.send_command("native.call", {
                "module": "channels", "method": "channelCount", "args": [True],
            })
            final_context = fl_list_capabilities()
            result["channel_verified"] = bool(
                observed_context.get("success")
                and observed_context.get("context") == capabilities["context"]
                and final_context.get("success")
                and final_context.get("context") == capabilities["context"]
                and result.get("context") == capabilities["context"]
                and observed_type.get("success") and observed_type.get("result") == 0
                and observed_name.get("success")
                and observed_name.get("result") == result.get("channel_name")
                and observed_count.get("success")
                and observed_count.get("result") == count["result"] + 1
            )
        except RuntimeError as exc:
            result["channel_verified"] = False
            result["readback_error"] = str(exc)
        # Close the loop: read the new channel's sample source from a save-copy
        # and compare it with the requested file. Only then is the insertion
        # "verified" instead of "partial".
        if result.get("channel_verified"):
            import os as _os
            import tempfile as _tempfile
            saved = None
            try:
                handle, temp_name = _tempfile.mkstemp(prefix="flmcp-sampler-", suffix=".flp")
                _os.close(handle)
                saved = Path(temp_name)
                copy = fl_save_project_copy(str(saved))
                if not copy.get("success"):
                    result["source_path_readback_error"] = (
                        copy.get("error") or "save-copy failed")
                else:
                    sources = parse_flp_sampler_sources(saved.read_bytes())
                    observed = next(
                        (item for item in sources if item.get("channel_index") == count["result"]),
                        None)
                    observed_path = observed.get("sample_path") if observed else None
                    result["source_path_readback"] = observed_path
                    matched = bool(
                        observed_path
                        and _os.path.normcase(_os.path.normpath(str(observed_path)))
                        == _os.path.normcase(_os.path.normpath(str(source))))
                    result["source_path_verified"] = matched
                    if matched:
                        result["state"] = "verified"
                        result["success"] = True
                        # The native layer's partial-stage message no longer applies.
                        result["error"] = None
            except (OSError, ValueError, RuntimeError) as exc:
                result["source_path_readback_error"] = str(exc)
            finally:
                if saved is not None:
                    saved.unlink(missing_ok=True)
    return result

def fl_add_generator(
    plugin_name: str, preset_path: str | None = None, dry_run: bool = False,
) -> dict:
    """Append a Generator (plugin-hosting channel) and verify the loaded plugin.

    plugin_name names a generator in FL's Plugin Database (e.g. "3x Osc"), or
    preset_path selects an explicit absolute .fst. Unlike fl_add_sampler this
    reports success=True with source_path_verified=True, because the loaded
    plugin's display name is read back from the channel and compared. FL must
    be stopped. dry_run resolves and validates without writing.
    """
    if (not isinstance(plugin_name, str) or not plugin_name.strip()
            or (preset_path is not None and not isinstance(preset_path, str))
            or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid plugin name or dry_run"}
    try:
        host = fl_list_capabilities()
        native = host.get("native", {})
        if (not host.get("success") or not native.get("available") or
                "channel.add_generator" not in native.get("operations", [])):
            return {"success": False, "submitted": False,
                    "error": (native.get("error")
                              or "Generator creation is unavailable on this bridge")}
        source = _generator_preset(plugin_name.strip(), native["engine_path"], preset_path)
    except (OSError, ValueError, RuntimeError, EOFError) as exc:
        return {"success": False, "submitted": False, "error": str(exc)}
    try:
        conn = get_connection()
        count = conn.send_command("native.call", {
            "module": "channels", "method": "channelCount", "args": [True],
        })
        if not count.get("success") or type(count.get("result")) is not int:
            return {"success": False, "submitted": False,
                    "error": count.get("error") or "Cannot read the current channel count"}
        request_id = uuid4().hex
        result = conn.send_command("native.addGenerator", {
            "request_id": request_id, "context": host["context"],
            "expected_count": count["result"], "path": str(source),
            "plugin_name": plugin_name.strip(), "dry_run": dry_run,
        }, timeout=30.0)
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id,
                "error": result.get("error") or "Response was not matched"}
    if result.get("submitted") and result.get("state") == "verified":
        try:
            observed_context = fl_list_capabilities()
            observed_type = conn.send_command("native.call", {
                "module": "channels", "method": "getChannelType", "args": [count["result"], True],
            })
            observed_count = conn.send_command("native.call", {
                "module": "channels", "method": "channelCount", "args": [True],
            })
            result["channel_verified"] = bool(
                observed_context.get("success")
                and observed_context.get("context") == host["context"]
                and result.get("context") == host["context"]
                and observed_type.get("success") and observed_type.get("result") != 0
                and observed_count.get("success")
                and observed_count.get("result") == count["result"] + 1
            )
        except RuntimeError as exc:
            result["channel_verified"] = False
            result["readback_error"] = str(exc)
    return result

def fl_read_sampler_sources() -> dict:
    """Independently read every Sampler's WAV path from a project save-copy.

    This closes the long-standing gap where fl_add_sampler could create a
    Sampler but could not read back which file it loaded. FL serialises the
    source path as a UTF-16LE ChannelID.SamplePath event in the .flp, so this
    tool writes a disposable project copy through the verified save-copy op,
    scans it with a tolerant event reader (pyflp aborts on this build's binary
    Sampler events), and returns the paths mapped to Channel Rack indices.

    The live project is never saved over. FL must be stopped. The temporary
    copy is deleted after reading. Each returned entry has channel_index,
    channel_iid and sample_path.
    """
    import tempfile

    saved = None
    try:
        handle, name = tempfile.mkstemp(prefix="flmcp-sampler-sources-", suffix=".flp")
        import os as _os
        _os.close(handle)
        saved = Path(name)
        copy = fl_save_project_copy(str(saved))
        if not copy.get("success"):
            return {"success": False, "error": copy.get("error") or "Project save-copy failed"}
        if not saved.is_file():
            return {"success": False, "error": "Save-copy did not produce a readable file"}
        sources = parse_flp_sampler_sources(saved.read_bytes())
        return {"success": True, "sources": sources, "count": len(sources),
                "context": copy.get("context")}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc)}
    finally:
        if saved is not None:
            try:
                saved.unlink()
            except OSError:
                pass

def fl_clone_mixer_effect_full(
    source_track: int, source_slot: int, target_track: int, target_slot: int,
    dry_run: bool = False,
) -> dict:
    """Clone a Mixer effect with its COMPLETE plugin state.

    Reads the source slot's full event-213 record from a project save-copy, then
    rebuilds the plugin's own factory FST skeleton with that record substituted
    for the skeleton's default state, loads it into an empty target slot and
    compares the target's event-213 SHA-256 with the source. A matching digest
    means the whole saved state — including opaque plugin chunks that are not
    exposed as named parameters — was carried over, not just the public knobs.

    The synthetic FST is written into the writable user Plugin-database Effects
    folder (the loader only accepts presets from a Plugin Database) and deleted
    afterwards. FL must be stopped. The target slot must be empty. This creates a
    new plugin instance; FL's undo support for a preset load is not assumed.
    """
    import hashlib
    import tempfile

    if any(type(v) is not int for v in (source_track, source_slot, target_track, target_slot)) \
            or not 0 <= source_track <= 500 or not 0 <= target_track <= 500 \
            or not 0 <= source_slot <= 9 or not 0 <= target_slot <= 9 \
            or type(dry_run) is not bool:
        return {"success": False, "error": "Invalid track/slot indices or dry_run"}
    if (source_track, source_slot) == (target_track, target_slot):
        return {"success": False, "error": "Source and target slot are the same"}
    capabilities = fl_list_capabilities()
    pinned = _pin(capabilities)
    if pinned is None:
        native = capabilities.get("native", {})
        return {"success": False, "error": native.get("error") or "Native bridge unavailable",
                "steps": [], "status_request_ids": []}
    context, engine_path = pinned
    import time
    steps: list[dict] = []
    saved = None
    synth = None
    after_copy = None

    def finish(result: dict) -> dict:
        """Attach correlation data and release artifacts when requests are final."""
        result.setdefault("steps", steps)
        result.setdefault("status_request_ids", _status_request_ids(steps))
        retained = _release_when_final(
            [p for p in (saved, after_copy, synth) if p is not None], steps)
        if retained:
            result["retained_artifacts"] = retained
        return result

    conn = get_connection()
    try:
        id_request = uuid4().hex
        plugin_name = conn.send_command("native.call", {
            "request_id": id_request, "module": "plugins", "method": "getPluginName",
            "args": [source_track, source_slot, False, True],
        }, timeout=15.0)
    except RuntimeError as exc:
        return finish({"success": False, "error": str(exc)})
    steps.append(_step_record("identify_source", id_request, plugin_name))
    if not plugin_name.get("success") or not isinstance(plugin_name.get("result"), str):
        return finish({"success": False,
                       "error": plugin_name.get("error") or "Source slot is empty"})
    name = plugin_name["result"]
    try:
        busy_request = uuid4().hex
        target_busy = conn.send_command("native.call", {
            "request_id": busy_request, "module": "plugins", "method": "isValid",
            "args": [target_track, target_slot, True],
        }, timeout=15.0)
    except RuntimeError as exc:
        return finish({"success": False, "error": str(exc)})
    steps.append(_step_record("check_target", busy_request, target_busy))
    if target_busy.get("success") and target_busy.get("result"):
        return finish({"success": False, "error": "Target slot is occupied"})

    try:
        handle, temp_name = tempfile.mkstemp(prefix="flmcp-fxstate-", suffix=".flp")
        import os as _os
        _os.close(handle)
        saved = Path(temp_name)
        copy = fl_save_project_copy(str(saved))
        steps.append(_step_record("capture_state", copy.get("request_id", ""), copy))
        if not copy.get("success") or copy.get("context") != context:
            return finish({"success": False,
                           "error": copy.get("error") or "Project or controller changed before capture"})
        parsed = parse_flp_plugin_states(saved.read_bytes())
        state = parsed["mixer_slots"].get((source_track, source_slot))
        if not state:
            return finish({"success": False,
                           "error": "The source slot stores no plugin state record"})
        skeleton_path = _effect_preset(name, engine_path, None)
        skeleton = skeleton_path.read_bytes()
        synthetic = _fst_with_plugin_state(skeleton, state)
        plan = {
            "success": True, "submitted": False, "dry_run": dry_run,
            "plugin_name": name, "skeleton": str(skeleton_path),
            "skeleton_bytes": len(skeleton), "state_bytes": len(state),
            "source_sha256": hashlib.sha256(state).hexdigest(),
            "source": {"track": source_track, "slot": source_slot},
            "target": {"track": target_track, "slot": target_slot},
            "synthetic_bytes": len(synthetic),
            "context": context,
        }
        if dry_run:
            return finish(plan)
        out_dir = (Path.home() / "Documents/Image-Line/FL Studio/Presets"
                   / "Plugin database/Effects")
        out_dir.mkdir(parents=True, exist_ok=True)
        synth = out_dir / ("FLMCP-Clone-" + uuid4().hex[:10] + ".fst")
        synth.write_bytes(synthetic)

        class _Conn:
            def send_command(self, action, params=None, timeout=10):
                return get_connection().send_command(action, params or {}, timeout=timeout)

        loaded = load_effect_at_slot(_Conn(), name, target_track, target_slot, False,
                                     preset_path=str(synth), context=context,
                                     engine_path=engine_path)
        steps.append(_step_record("load_state", loaded.get("effect_request_id", ""), loaded,
                                  {"track": target_track, "slot": target_slot}))
        if loaded.get("outcome") == "outcome_unknown" or loaded.get("write_outcome_unknown"):
            # Never stage a compensating write until the original is resolved.
            return finish({**plan, "success": False, "submitted": True,
                           "state": "outcome_unknown",
                           "error": loaded.get("error") or "Load result is unknown"})
        if not loaded.get("verified"):
            return finish({**plan, "success": False, "submitted": bool(loaded.get("submitted")),
                           "error": loaded.get("error") or "Synthetic preset did not load"})
        # Independent comparison of the target's full state record.
        time.sleep(0.2)
        after_copy = synth.with_suffix(".after.flp")
        copy2 = fl_save_project_copy(str(after_copy))
        steps.append(_step_record("verify_state", copy2.get("request_id", ""), copy2))
        if not copy2.get("success") or copy2.get("context") != context:
            return finish({**plan, "success": False, "submitted": True,
                           "error": copy2.get("error") or "Verification save-copy failed"})
        parsed2 = parse_flp_plugin_states(after_copy.read_bytes())
        target_state = parsed2["mixer_slots"].get((target_track, target_slot))
        if not target_state:
            return finish({**plan, "success": False, "submitted": True,
                           "error": "Target slot stores no state record after load"})
        digest_match = hashlib.sha256(target_state).hexdigest() == plan["source_sha256"]
        return finish({**plan, "submitted": True, "success": bool(digest_match),
                       "target_sha256": hashlib.sha256(target_state).hexdigest(),
                       "target_bytes": len(target_state),
                       "state_matched": digest_match,
                       "note": ("Complete event-213 state matched the source"
                                if digest_match else
                                "State digest differs; the plugin may normalise or re-serialise")})
    except (OSError, ValueError, RuntimeError) as exc:
        return finish({"success": False, "submitted": synth is not None, "error": str(exc)})

def _load_state_into_slot(plugin: str, state: bytes, track: int, slot: int,
                          engine_path: str, context: str | None = None,
                          synth_path: Path | None = None,
                          lease_token: str | None = None) -> dict:
    """Load a captured event-213 record into an empty Mixer slot.

    A caller that has already staged the synthetic FST passes ``synth_path``;
    otherwise one is created here and removed after the request is final.
    The pinned ``context`` is forwarded so a controller/project change is
    refused before the write instead of being silently replaced.
    """
    if synth_path is None:
        skeleton_path = _effect_preset(plugin, engine_path, None)
        synthetic = _fst_with_plugin_state(skeleton_path.read_bytes(), state)
        out_dir = (Path.home() / "Documents/Image-Line/FL Studio/Presets"
                   / "Plugin database/Effects")
        out_dir.mkdir(parents=True, exist_ok=True)
        synth = out_dir / ("FLMCP-State-" + uuid4().hex[:10] + ".fst")
        synth.write_bytes(synthetic)
        owned = True
    else:
        synth = synth_path
        owned = False

    class _Conn:
        def send_command(self, action, params=None, timeout=10):
            return get_connection().send_command(action, params or {}, timeout=timeout)

    try:
        result = load_effect_at_slot(_Conn(), plugin, track, slot, False,
                                     preset_path=str(synth), context=context,
                                     engine_path=engine_path, lease_token=lease_token)
        return {"success": bool(result.get("verified")),
                "submitted": bool(result.get("submitted")),
                "verified": bool(result.get("verified")),
                "write_outcome_unknown": bool(result.get("write_outcome_unknown")
                                              or result.get("outcome") == "outcome_unknown"),
                "effect_request_id": result.get("effect_request_id"),
                "queue_request_id": result.get("queue_request_id"),
                "error": None if result.get("verified") else
                (result.get("error") or "preset did not load")}
    finally:
        if owned:
            synth.unlink(missing_ok=True)

def fl_swap_mixer_effects(track: int, slot_a: int, slot_b: int, dry_run: bool = False) -> dict:
    """Swap two Mixer FX slots with their COMPLETE plugin state.

    Reads both slots' full event-213 records, removes each plugin and reloads it
    into the other slot from a synthetic preset carrying the original state, then
    verifies each slot's digest now matches the other's original. If a step
    fails, it attempts to restore both slots from the captured records.

    Removal is destructive on this build (FL registers no restorable undo state),
    so this reports irreversible=true. FL must be stopped. Track 0 is Master.
    """
    import hashlib
    import time

    if any(type(v) is not int for v in (track, slot_a, slot_b)) or not 0 <= track <= 500 \
            or not 0 <= slot_a <= 9 or not 0 <= slot_b <= 9 or slot_a == slot_b \
            or type(dry_run) is not bool:
        return {"success": False, "error": "Invalid track/slots or dry_run"}
    capabilities = fl_list_capabilities()
    pinned = _pin(capabilities)
    if pinned is None:
        native = capabilities.get("native", {})
        return {"success": False, "error": native.get("error") or "Native bridge unavailable",
                "irreversible": True, "steps": [], "status_request_ids": []}
    context, engine_path = pinned
    import tempfile

    # Outcome states. Kept as plain strings so existing response fields stay
    # compatible; `state` is additive.
    NOT_WRITTEN = "not_written"           # nothing reached FL
    SWAP_VERIFIED = "swap_verified"       # both digests cross-matched
    ORIGINAL_RESTORED = "original_restored"  # both originals verified after repair
    OUTCOME_UNKNOWN = "outcome_unknown"   # a write may still be in flight
    PARTIAL = "partial_modification"      # known write, state not verified/repaired

    steps: list[dict] = []
    saved = None
    synths: list[Path] = []
    verify_path = None
    restore_paths: list[Path] = []
    staged: dict[str, Path] = {}          # preset kind -> validated FST
    project_verified = False              # only true after a successful readback
    lease: dict[str, str] = {}            # {"token": ...} once slots are claimed
    conn = get_connection()

    def finish(result: dict) -> dict:
        nonlocal project_verified
        result.setdefault("steps", steps)
        result.setdefault("status_request_ids", _status_request_ids(steps))
        result.setdefault("irreversible", True)
        # A verified repair wins over a generic partial failure, but never
        # overstates the swap itself (success stays False).
        if result.get("restored") is True:
            result.setdefault("state", ORIGINAL_RESTORED)
            project_verified = True
        result.setdefault("state", PARTIAL)
        # Recovery material must outlive a failed or unverifiable readback:
        # deleting it here would leave the caller with no way to recover.
        if project_verified:
            retained = _release_when_final(
                [p for p in (saved, verify_path, *restore_paths, *synths) if p is not None],
                steps)
        else:
            retained = [str(p) for p in
                        (saved, verify_path, *restore_paths, *synths)
                        if p is not None and p.exists()]
        if retained:
            result["retained_artifacts"] = retained
        release_leases()
        return result

    def release_leases() -> None:
        token = lease.get("token")
        if not token:
            return
        for slot in (slot_a, slot_b):
            try:
                conn.send_command("native.releaseMixerSlot", {
                    "request_id": uuid4().hex, "context": context,
                    "track": track, "slot": slot, "lease_token": token,
                }, timeout=15.0)
            except (OSError, ValueError, RuntimeError):
                pass  # lease also expires on its own TTL

    def plugin_of(slot: int, label: str) -> str | None:
        request_id = uuid4().hex
        response = conn.send_command("native.call", {
            "request_id": request_id, "module": "plugins", "method": "getPluginName",
            "args": [track, slot, False, True],
        }, timeout=15.0)
        steps.append(_step_record(label, request_id, response,
                                  {"track": track, "slot": slot}))
        return response.get("result") if response.get("success") else None

    def current_state(path: Path, label: str) -> dict | None:
        copy = fl_save_project_copy(str(path))
        steps.append(_step_record(label, copy.get("request_id", ""), copy))
        if not copy.get("success") or copy.get("context") != context:
            return None
        return parse_flp_plugin_states(path.read_bytes())["mixer_slots"]

    def digest(value: bytes | None) -> str | None:
        return hashlib.sha256(value).hexdigest() if value else None

    def remove(slot: int, label: str) -> dict:
        request_id = uuid4().hex
        response = conn.send_command("native.removeMixerEffect", {
            "request_id": request_id, "context": context, "track": track,
            "slot": slot, "dry_run": False,
            **({"lease_token": token} if (token := lease.get("token")) else {}),
        }, timeout=40.0)
        steps.append(_step_record(label, request_id, response,
                                  {"track": track, "slot": slot}))
        return response

    def stage_presets(plan_data: dict) -> bool:
        """Build and validate every preset BEFORE the first destructive write.

        Four presets are needed: forward A<-B and B<-A, plus the two restore
        presets. Building them at load time after a slot was removed can fail
        with no recovery material left; staging once here makes the whole
        write sequence replayable from files that were already proven valid.
        """
        out_dir = (Path.home() / "Documents/Image-Line/FL Studio/Presets"
                   / "Plugin database/Effects")
        out_dir.mkdir(parents=True, exist_ok=True)
        wanted = {
            "forward_a": (plan_data["b"]["plugin"], states[slot_b], plan_data["b"]["sha256"]),
            "forward_b": (plan_data["a"]["plugin"], states[slot_a], plan_data["a"]["sha256"]),
            "restore_a": (plan_data["a"]["plugin"], states[slot_a], plan_data["a"]["sha256"]),
            "restore_b": (plan_data["b"]["plugin"], states[slot_b], plan_data["b"]["sha256"]),
        }
        for kind, (plugin, state, want_sha) in wanted.items():
            try:
                synthetic = _fst_with_plugin_state(
                    _effect_preset(plugin, engine_path, None).read_bytes(), state)
                # Verify the FST carries exactly the intended event-213 payload.
                events = _flp_event_layout(synthetic)[2]
                records = [e["payload"] for e in events if e["id"] == 213]
            except (OSError, ValueError, TypeError):
                return False
            if not records or digest(records[0]) != want_sha:
                return False
            path = out_dir / ("FLMCP-SwapStage-" + uuid4().hex[:12] + ".fst")
            path.write_bytes(synthetic)
            staged[kind] = path
            synths.append(path)
        return True

    def load(kind: str, plugin: str, state: bytes, slot: int, label: str) -> dict:
        request_id = uuid4().hex
        synth = staged[kind]   # staged and validated before any write
        result = _load_state_into_slot(plugin, state, track, slot, engine_path,
                                       context=context, synth_path=synth,
                                       lease_token=lease.get("token"))
        steps.append(_step_record(label, result.get("effect_request_id") or request_id,
                                  {"success": result["success"],
                                   "submitted": result["submitted"],
                                   "queue_request_id": result.get("queue_request_id"),
                                   "error": result.get("error"),
                                   **({"error_type": "OutcomeUnknown"}
                                      if result.get("write_outcome_unknown") else {})},
                                  {"track": track, "slot": slot}))
        return result

    def restore_if_permitted(reason: str) -> dict:
        """Restore both slots only after writes are final and state is confirmed.

        Three gates, in order: every submitted request must have left the
        queue, the pinned instance/project context must still match, and a
        fresh save-copy must confirm the current slot states. Without all
        three, a compensation could replay an in-flight write into a moved
        project. Both original digests are then checked independently before
        ``restored`` may be reported.
        """
        response: dict = {"restore_reason": reason, "restored": False}
        if not _terminal(steps):
            response["restore_blocked"] = "submitted requests have not finished"
            return response
        # The observed state can only be attributed to this swap if our slot
        # leases are still held; another writer editing either slot in between
        # makes a replay unsafe even when the digests happen to differ.
        token = lease.get("token")
        if token:
            for check_slot in (slot_a, slot_b):
                held = conn.send_command("native.inspectMixerSlotLease", {
                    "request_id": uuid4().hex, "context": context,
                    "track": track, "slot": check_slot, "lease_token": token,
                }, timeout=15.0)
                if not held.get("held_by_this_write"):
                    response["restore_blocked"] = "slot lease no longer held by this swap"
                    return response
        fresh = fl_list_capabilities()
        if (not fresh.get("success") or fresh.get("context") != context):
            response["restore_blocked"] = "instance, controller or project changed"
            return response
        restore_copy = saved.with_suffix(".restore.flp")
        restore_paths.append(restore_copy)
        observed = current_state(restore_copy, "restore_readback")
        if observed is None:
            response["restore_blocked"] = "could not read back slot state"
            return response
        observed_a = observed.get((track, slot_a))
        observed_b = observed.get((track, slot_b))
        response["pre_restore_a_sha256"] = digest(observed_a)
        response["pre_restore_b_sha256"] = digest(observed_b)

        # Restore only slots whose state no longer matches the capture.
        repairs = []
        if digest(observed_a) != plan["a"]["sha256"]:
            repairs.append((slot_a, names[slot_a], states[slot_a], plan["a"]["sha256"], "a"))
        if digest(observed_b) != plan["b"]["sha256"]:
            repairs.append((slot_b, names[slot_b], states[slot_b], plan["b"]["sha256"], "b"))
        for slot, plugin, state, _want, label in repairs:
            occupied = digest(observed.get((track, slot))) is not None
            if occupied:
                removed = remove(slot, "restore_remove_" + label)
                if (_step_outcome(removed) == "outcome_unknown"
                        or not removed.get("success")):
                    response["restore_blocked"] = "restore removal failed or is unknown"
                    return response
            loaded = load("restore_" + label, plugin, state, slot,
                          "restore_load_" + label)
            if (loaded.get("write_outcome_unknown") or not loaded["success"]):
                response["restore_blocked"] = "restore load failed or is unknown"
                return response

        time.sleep(0.2)
        verify_path_local = saved.with_suffix(".restore_verify.flp")
        restore_paths.append(verify_path_local)
        final = current_state(verify_path_local, "restore_verify")
        if final is None:
            response["restore_blocked"] = "could not verify restored slots"
            return response
        final_a, final_b = final.get((track, slot_a)), final.get((track, slot_b))
        response["a_after_sha256"], response["b_after_sha256"] = digest(final_a), digest(final_b)
        # Both originals independently verified before claiming recovery.
        response["restored"] = bool(digest(final_a) == plan["a"]["sha256"]
                                    and digest(final_b) == plan["b"]["sha256"])
        if response["restored"]:
            response["note"] = "Both slots restored from the captured event-213 records"
        else:
            response["restore_blocked"] = "restored digests do not match both originals"
        return response

    try:
        names = {slot_a: plugin_of(slot_a, "identify_a"),
                 slot_b: plugin_of(slot_b, "identify_b")}
        missing = [s for s, name in names.items() if not isinstance(name, str) or not name]
        if missing:
            return finish({"success": False, "error": "Slot(s) %s are empty" % missing})

        handle, temp_name = tempfile.mkstemp(prefix="flmcp-swap-", suffix=".flp")
        import os as _os
        _os.close(handle)
        saved = Path(temp_name)
        slots = current_state(saved, "capture_state")
        if slots is None:
            return finish({"success": False, "error": "Save-copy failed or context changed"})
        states = {slot_a: slots.get((track, slot_a)), slot_b: slots.get((track, slot_b))}
        if not states[slot_a] or not states[slot_b]:
            return finish({"success": False, "error": "A slot stores no state record"})
        plan = {
            "success": True, "submitted": False, "dry_run": dry_run,
            "track": track, "slot_a": slot_a, "slot_b": slot_b,
            "context": context,
            "a": {"plugin": names[slot_a], "bytes": len(states[slot_a]),
                  "sha256": digest(states[slot_a])},
            "b": {"plugin": names[slot_b], "bytes": len(states[slot_b]),
                  "sha256": digest(states[slot_b])},
            "warning": ("Removal has no restorable FL undo; "
                        "a failure is recovered from the captured records."),
        }
        if dry_run:
            return finish(plan)

        # Stage AND validate all four presets before the first destructive call.
        # Automation targets are location-encoded on this build. Swapping the
        # plugins changes what an existing event ID resolves to; FL's exact
        # post-swap binding has NOT been verified here, so any affected link
        # without a corroborated current binding refuses the structural write.
        scan_id = uuid4().hex
        scan = conn.send_command("native.scanMixerAutomationLinks", {
            "request_id": scan_id, "context": context,
            "track": track, "slots": [slot_a, slot_b],
        }, timeout=15.0)
        affected = scan.get("affected") if scan.get("success") else None
        plan["automation_precheck"] = {
            "available": affected is not None,
            "affected_links": affected or [],
            "post_swap_resolution_verified": False,
        }
        if affected is None:
            return finish({**plan, "success": False, "state": NOT_WRITTEN,
                           "error": scan.get("error") or "Automation target precheck failed"})
        unconfirmed = [link for link in affected if not link.get("binding_confirmed")]
        if unconfirmed:
            return finish({**plan, "success": False, "state": NOT_WRITTEN,
                           "error": "%d automation link(s) target these slots without a "
                                    "confirmed binding; refusing structural swap"
                                    % len(unconfirmed)})
        # Staging must succeed before the first destructive call.
        if not stage_presets(plan):
            return finish({**plan, "success": False, "state": NOT_WRITTEN,
                           "error": "Could not stage validated swap/restore presets"})

        # Host-side arbitration: claim both slots for this compound write.
        lease_token = uuid4().hex
        lease["token"] = lease_token
        claim = conn.send_command("native.claimMixerSlot", {
            "request_id": uuid4().hex, "context": context,
            "track": track, "slot": slot_a, "lease_token": lease_token,
        }, timeout=15.0)
        if not claim.get("success"):
            return finish({**plan, "success": False, "state": NOT_WRITTEN,
                           "error": claim.get("error") or "Could not claim slot A"})
        claim_b = conn.send_command("native.claimMixerSlot", {
            "request_id": uuid4().hex, "context": context,
            "track": track, "slot": slot_b, "lease_token": lease_token,
        }, timeout=15.0)
        if not claim_b.get("success"):
            conn.send_command("native.releaseMixerSlot", {
                "request_id": uuid4().hex, "context": context,
                "track": track, "slot": slot_a, "lease_token": lease_token,
            }, timeout=15.0)
            return finish({**plan, "success": False, "state": NOT_WRITTEN,
                           "error": claim_b.get("error") or "Could not claim slot B"})

        # A <- B's state, B <- A's state. Stop on the first unknown result.
        r1 = remove(slot_a, "remove_a")
        if _step_outcome(r1) == "outcome_unknown":
            return finish({**plan, "success": False, "submitted": True,
                           "state": OUTCOME_UNKNOWN, "error": "remove A result is unknown"})
        if not r1.get("success"):
            return finish({**plan, "success": False, "state": NOT_WRITTEN,
                           "error": r1.get("error") or "remove A failed"})
        l1 = load("forward_a", names[slot_b], states[slot_b], slot_a, "load_b_into_a")
        if l1.get("write_outcome_unknown"):
            # Never compensate while the original write may still be in flight.
            return finish({**plan, "success": False, "submitted": True,
                           "state": OUTCOME_UNKNOWN, "error": "load B into A is unknown"})
        if not l1["success"]:
            result = {**plan, "success": False, "submitted": True,
                      "state": PARTIAL,
                      "error": l1.get("error") or "load B into A failed"}
            result.update(restore_if_permitted("load_b_into_a_failed"))
            return finish(result)
        r2 = remove(slot_b, "remove_b")
        if _step_outcome(r2) == "outcome_unknown":
            return finish({**plan, "success": False, "submitted": True,
                           "state": OUTCOME_UNKNOWN, "error": "remove B result is unknown"})
        if not r2.get("success"):
            result = {**plan, "success": False, "submitted": True,
                      "state": PARTIAL,
                      "error": r2.get("error") or "remove B failed; A now holds B"}
            result.update(restore_if_permitted("remove_b_failed"))
            return finish(result)
        l2 = load("forward_b", names[slot_a], states[slot_a], slot_b, "load_a_into_b")
        if l2.get("write_outcome_unknown"):
            return finish({**plan, "success": False, "submitted": True,
                           "state": OUTCOME_UNKNOWN, "error": "load A into B is unknown"})
        if not l2["success"]:
            result = {**plan, "success": False, "submitted": True,
                      "state": PARTIAL,
                      "error": l2.get("error") or "load A into B failed; B empty"}
            result.update(restore_if_permitted("load_a_into_b_failed"))
            return finish(result)

        time.sleep(0.2)
        verify_path = saved.with_suffix(".verify.flp")
        after = current_state(verify_path, "verify_state")
        if after is None:
            # Writes finished but the project could NOT be verified. Keep every
            # recovery artifact; the caller must be able to inspect or repair.
            return finish({**plan, "success": False, "submitted": True,
                           "state": PARTIAL,
                           "error": "Verification save-copy failed or context changed",
                           "note": "Project state unverified; recovery material retained"})
        project_verified = True
        a_after, b_after = after.get((track, slot_a)), after.get((track, slot_b))
        matched = bool(a_after and b_after
                       and digest(a_after) == plan["b"]["sha256"]
                       and digest(b_after) == plan["a"]["sha256"])
        if matched:
            return finish({**plan, "submitted": True, "success": True,
                           "state": SWAP_VERIFIED, "state_matched": True,
                           "a_after_sha256": digest(a_after),
                           "b_after_sha256": digest(b_after),
                           "note": "Slots swapped with complete state"})
        result = {**plan, "submitted": True, "success": False, "state_matched": False,
                  "a_after_sha256": digest(a_after), "b_after_sha256": digest(b_after),
                  "note": "Digests did not cross-match; repair attempted from capture"}
        result.update(restore_if_permitted("digest_mismatch"))
        return finish(result)
    except (OSError, ValueError, RuntimeError) as exc:
        return finish({"success": False, "submitted": any(
            step.get("submitted") for step in steps), "state": PARTIAL, "error": str(exc)})


def fl_read_mixer_effect_state(track: int | None = None, slot: int | None = None) -> dict:
    """Read a Mixer effect slot's complete plugin state from a project save-copy.

    FL serialises each occupied FX slot's full plugin data (including opaque
    plugin chunks that are not exposed as named parameters) as event 213 in the
    .flp. This tool writes a disposable save-copy, extracts those records and
    returns a summary. Pass track and slot to get one slot's state (base64 +
    sha256 + byte length); omit both to list every slot with its state digest.
    track 0 is Master. The live project is never saved over; the temporary copy
    is deleted after reading. FL must be stopped.

    This is the read side of effect-slot cloning: it can prove whether two slots
    hold identical plugin state, but it does not itself generate a loadable FST.
    """
    import base64
    import hashlib
    import tempfile

    if (track is not None and type(track) is not int) or \
       (slot is not None and type(slot) is not int):
        return {"success": False, "error": "track and slot must be integers"}
    if (track is None) != (slot is None):
        return {"success": False, "error": "Pass both track and slot, or neither"}
    if track is not None and (track < 0 or track > 500):
        return {"success": False, "error": "Invalid Mixer track"}
    if slot is not None and (slot < 0 or slot >= 10):
        return {"success": False, "error": "Invalid effect slot"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if (not capabilities.get("success") or not native.get("available")
            or "project.save_copy" not in native.get("operations", [])):
        return {"success": False,
                "error": native.get("error") or "Project save-copy is unavailable on this bridge"}
    saved = None
    try:
        handle, name = tempfile.mkstemp(prefix="flmcp-fx-state-", suffix=".flp")
        import os as _os
        _os.close(handle)
        saved = Path(name)
        copy = fl_save_project_copy(str(saved))
        if not copy.get("success"):
            return {"success": False, "error": copy.get("error") or "Project save-copy failed"}
        parsed = parse_flp_plugin_states(saved.read_bytes())
        slots = parsed["mixer_slots"]
        if track is None:
            listing = [
                {"track": t, "slot": s, "bytes": len(data),
                 "sha256": hashlib.sha256(data).hexdigest()}
                for (t, s), data in sorted(slots.items())
            ]
            return {"success": True, "framing": parsed["framing"],
                    "version": parsed["version"], "mixer_slots": listing,
                    "count": len(listing), "context": copy.get("context")}
        # The FLP insert index is 0 = Master, then tracks in physical order.
        # The public track index matches the insert index on the verified build,
        # but confirm the record exists before claiming a state.
        data = slots.get((track, slot))
        if data is None:
            return {"success": False,
                    "error": "The slot has no plugin state (empty or out of range)",
                    "track": track, "slot": slot, "framing": parsed["framing"],
                    "version": parsed["version"]}
        return {"success": True, "track": track, "slot": slot,
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "state_base64": base64.b64encode(data).decode("ascii"),
                "framing": parsed["framing"], "version": parsed["version"],
                "context": copy.get("context")}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"success": False, "error": str(exc)}
    finally:
        if saved is not None:
            try:
                saved.unlink()
            except OSError:
                pass

def fl_replace_sampler_sample(channel: int, path: str, dry_run: bool = False) -> dict:
    """Replace the WAV in an existing Sampler, verified by source-path readback.

    channel is the global Channel Rack index of a Sampler. path is an existing
    absolute WAV. The live project is never saved over; the two save-copies used
    for before/after readback are temporary. On this build the sample load
    records its own FL undo entry (confirmed by the tool's acceptance run), so
    a single undoUp restores the previous path. FL must be stopped.

    The result reports the previous and new source paths and whether every
    other Sampler's path was left unchanged. dry_run validates without writing.
    """
    if (type(channel) is not int or channel < 0 or not isinstance(path, str)
            or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "channel": channel,
                "error": "Invalid channel, path, or dry_run"}
    try:
        source = Path(path)
        if not source.is_absolute() or source.suffix.casefold() != ".wav":
            raise ValueError("Use an absolute WAV file path")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("WAV path is not a file")
        with wave.open(str(source), "rb") as audio:
            if audio.getnchannels() < 1 or audio.getframerate() < 1 or audio.getnframes() < 1:
                raise ValueError("WAV file has no audio frames")
    except (OSError, ValueError, EOFError, wave.Error) as exc:
        return {"success": False, "submitted": False, "channel": channel, "error": str(exc)}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if (not capabilities.get("success") or not native.get("available") or
            "channel.replace_sample" not in native.get("operations", [])):
        return {"success": False, "submitted": False, "channel": channel,
                "error": (native.get("error")
                          or "Sampler sample replacement is unavailable on this bridge")}
    before = fl_read_sampler_sources()
    if not before.get("success"):
        return {"success": False, "submitted": False, "channel": channel,
                "error": before.get("error") or "Cannot read the current Sampler sources"}
    prior = next((s for s in before["sources"] if s.get("channel_index") == channel), None)
    if prior is None:
        return {"success": False, "submitted": False, "channel": channel,
                "error": "The target channel has no readable sample source (not a Sampler?)"}
    previous_path = prior["sample_path"]
    if previous_path.casefold() == str(source).casefold():
        return {"success": False, "submitted": False, "channel": channel,
                "error": "The Sampler already has this sample loaded"}
    try:
        conn = get_connection()
        count = conn.send_command("native.call", {
            "module": "channels", "method": "channelCount", "args": [True],
        })
        if not count.get("success") or type(count.get("result")) is not int:
            return {"success": False, "submitted": False, "channel": channel,
                    "error": count.get("error") or "Cannot read the current channel count"}
        request_id = uuid4().hex
        result = conn.send_command("native.replaceSample", {
            "request_id": request_id, "context": capabilities["context"],
            "channel": channel, "expected_count": count["result"],
            "path": str(source), "dry_run": dry_run,
        }, timeout=45.0)
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run, "channel": channel,
                "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "channel": channel,
                "request_id": request_id,
                "error": result.get("error") or "Response was not matched"}
    if dry_run or not result.get("submitted"):
        return {**result, "request_id": request_id, "previous_path": previous_path}
    after = fl_read_sampler_sources()
    if not after.get("success"):
        return {**result, "channel": channel, "source_verified": False,
                "readback_error": after.get("error") or "Cannot read back Sampler sources"}
    new_entry = next((s for s in after["sources"] if s.get("channel_index") == channel), None)
    new_path = new_entry.get("sample_path") if new_entry else None
    source_verified = bool(result.get("success") and new_path and
                           new_path.casefold() == str(source).casefold())
    other_before = {s["channel_index"]: s["sample_path"] for s in before["sources"]
                    if s["channel_index"] != channel}
    other_after = {s["channel_index"]: s["sample_path"] for s in after["sources"]
                   if s["channel_index"] != channel}
    others_unchanged = other_before == other_after
    return {**result, "channel": channel, "previous_path": previous_path,
            "new_path": new_path, "source_verified": source_verified,
            "other_sources_unchanged": others_unchanged,
            "success": bool(source_verified and others_unchanged and result.get("success"))}

def fl_insert_audio_clip(
    channel: int, track: int, start_tick: int, length_tick: int,
    source_start: int | None = None, source_end: int | None = None, dry_run: bool = False,
) -> dict:
    """Place an existing Sampler channel as a Playlist Audio Clip.

    channel is the global Channel Rack index of a Sampler (see fl_add_sampler).
    track is one-based, start_tick and length_tick are PPQ ticks. The optional
    source window is also in ticks and must span exactly length_tick, because
    FL sizes a clip to its source window. The clip is read back from FL's
    Playlist objects and compared; audio source windows read back in song
    milliseconds. FL must be stopped. dry_run validates without writing.
    """
    if (type(channel) is not int or channel < 0
            or type(track) is not int or not 1 <= track <= 500
            or type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF
            or type(length_tick) is not int or not 1 <= length_tick <= 0x3FFFFFFF
            or (source_start is not None and (type(source_start) is not int or source_start < 0))
            or (source_end is not None and (type(source_end) is not int or source_end < 0))
            or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid audio clip request"}
    if (source_start is None) != (source_end is None):
        return {"success": False, "submitted": False, "error": "Invalid source range"}
    if source_start is None:
        # -1 keeps the source window that FL derives from the Sampler itself.
        source_start = -1
        source_end = -1
    elif length_tick != source_end - source_start:
        return {"success": False, "submitted": False,
                "error": "length_tick must equal the source window width"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "playlist.insert_clip" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Audio clip insertion is unavailable on this bridge"}
    context = capabilities["context"]
    page = fl_list_playlist_clips(offset=0, limit=1)
    if not page.get("success") or page.get("context") != context:
        return {"success": False, "submitted": False,
                "error": page.get("error") or "Cannot read the current Playlist clip count"}
    expected_count = page.get("count")
    if type(expected_count) is not int:
        return {"success": False, "submitted": False, "error": "Invalid Playlist clip count"}
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.insertAudioClip",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "channel": channel, "track": track, "start_tick": start_tick,
             "length_tick": length_tick, "source_start": source_start, "source_end": source_end,
             "expected_count": expected_count, "dry_run": dry_run},
            timeout=30.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    index = result.get("index")
    observed = fl_list_playlist_clips(offset=index, limit=1) if type(index) is int else {}
    clips = observed.get("clips", [])
    clip = clips[0] if len(clips) == 1 else {}
    verified = (
        observed.get("success") and observed.get("context") == context
        and observed.get("count") == expected_count + 1 and clip.get("index") == index
        and clip.get("track") == track and clip.get("start_tick") == start_tick
        and clip.get("length_tick") == length_tick
        and clip.get("source_type") == "channel" and clip.get("source_index") == channel
    )
    return {
        "success": bool(verified and result.get("success")),
        "submitted": True,
        "verified": bool(verified),
        "request_id": request_id,
        "context": context,
        "index": index,
        "observed": clip or None,
        **({"error": result.get("error") or "Audio clip insertion could not be verified"}
           if not verified or not result.get("success") else {}),
    }

def fl_insert_pattern_clip(
    pattern: int, track: int, start_tick: int, length_tick: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Place an existing Pattern as a Playlist Pattern Clip.

    pattern follows the patterns-module indexing and the native clip source
    field. length_tick defaults to FL's own derived pattern length in ticks.
    The clip is read back from FL's Playlist objects and compared. FL must be
    stopped. dry_run validates without writing.
    """
    if (type(pattern) is not int or pattern < 0
            or type(track) is not int or not 1 <= track <= 500
            or type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF
            or (length_tick is not None and
                (type(length_tick) is not int or not 1 <= length_tick <= 0x3FFFFFFF))
            or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid pattern clip request"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "playlist.insert_clip" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Pattern clip insertion is unavailable on this bridge"}
    context = capabilities["context"]
    page = fl_list_playlist_clips(offset=0, limit=1)
    if not page.get("success") or page.get("context") != context:
        return {"success": False, "submitted": False,
                "error": page.get("error") or "Cannot read the current Playlist clip count"}
    expected_count = page.get("count")
    if type(expected_count) is not int:
        return {"success": False, "submitted": False, "error": "Invalid Playlist clip count"}
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.insertPatternClip",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "pattern": pattern, "track": track, "start_tick": start_tick,
             "length_tick": length_tick, "expected_count": expected_count, "dry_run": dry_run},
            timeout=30.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    index = result.get("index")
    observed = fl_list_playlist_clips(offset=index, limit=1) if type(index) is int else {}
    clips = observed.get("clips", [])
    clip = clips[0] if len(clips) == 1 else {}
    verified = (
        observed.get("success") and observed.get("context") == context
        and observed.get("count") == expected_count + 1 and clip.get("index") == index
        and clip.get("track") == track and clip.get("start_tick") == start_tick
        and clip.get("source_type") == "pattern"
        and clip.get("source_index") == pattern
        and (length_tick is None or clip.get("length_tick") == length_tick)
    )
    return {
        "success": bool(verified and result.get("success")),
        "submitted": True,
        "verified": bool(verified),
        "request_id": request_id,
        "context": context,
        "index": index,
        "observed": clip or None,
        **({"error": result.get("error") or "Pattern clip insertion could not be verified"}
           if not verified or not result.get("success") else {}),
    }

def fl_insert_automation_clip(
    channel: int, track: int, start_tick: int, length_tick: int, dry_run: bool = False,
) -> dict:
    """Place an automation channel's control as a Playlist Automation Clip.

    channel is the global Channel Rack index of an automation channel
    (channel type 2). length_tick is required because automation clips have
    no derived length. The clip is read back and compared. FL must be
    stopped. dry_run validates without writing.
    """
    if (type(channel) is not int or channel < 0
            or type(track) is not int or not 1 <= track <= 500
            or type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF
            or type(length_tick) is not int or not 1 <= length_tick <= 0x3FFFFFFF
            or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid automation clip request"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "playlist.insert_clip" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Automation clip insertion is unavailable on this bridge"}
    context = capabilities["context"]
    page = fl_list_playlist_clips(offset=0, limit=1)
    if not page.get("success") or page.get("context") != context:
        return {"success": False, "submitted": False,
                "error": page.get("error") or "Cannot read the current Playlist clip count"}
    expected_count = page.get("count")
    if type(expected_count) is not int:
        return {"success": False, "submitted": False, "error": "Invalid Playlist clip count"}
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.insertAutomationClip",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "channel": channel, "track": track, "start_tick": start_tick,
             "length_tick": length_tick, "expected_count": expected_count, "dry_run": dry_run},
            timeout=30.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_create_automation_source(
    target_kind: str, track: int | None = None, slot: int | None = None,
    parameter: int | None = None, target_event_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Create an automation channel linked to a control event.

    The verified form is target_kind="mixer_effect_parameter" with track,
    zero-based slot and parameter (for example track=3, slot=0, parameter=1
    for the first effect's second parameter). target_kind="event_id" accepts a
    raw FL event id only when FL can read its name. FL appends the channel,
    registers the link, and the result reports both with independent readback.
    FL must be stopped. dry_run validates without writing.
    """
    if target_kind == "mixer_effect_parameter":
        target = {"kind": target_kind, "track": track, "slot": slot, "parameter": parameter}
        if not all(type(v) is int for v in (track, slot, parameter)):
            return {"success": False, "submitted": False,
                    "error": "track, slot and parameter are required integers"}
    elif target_kind == "event_id":
        target = {"kind": target_kind, "id": target_event_id}
        if type(target_event_id) is not int:
            return {"success": False, "submitted": False,
                    "error": "target_event_id must be an integer"}
    else:
        return {"success": False, "submitted": False,
                "error": "target_kind must be mixer_effect_parameter or event_id"}
    if type(dry_run) is not bool:
        return {"success": False, "submitted": False, "error": "dry_run must be a Boolean"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if (not capabilities.get("success") or not native.get("available")
            or "automation.create_source" not in native.get("operations", [])):
        return {"success": False, "submitted": False,
                "error": native.get("error")
                or "Automation source creation is unavailable on this bridge"}
    context = capabilities["context"]
    try:
        conn = get_connection()
        count = conn.send_command("native.call", {
            "module": "channels", "method": "channelCount", "args": [True],
        })
        if not count.get("success") or type(count.get("result")) is not int:
            return {"success": False, "submitted": False,
                    "error": count.get("error") or "Cannot read the current channel count"}
        request_id = uuid4().hex
        result = conn.send_command(
            "native.createAutomationSource",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "target": target, "expected_count": count["result"], "dry_run": dry_run},
            timeout=30.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if (result.get("submitted") and not result.get("success")
            and result.get("pending_identity") and type(result.get("index")) is int):
        # FL populates the new channel's identity on a later cycle; re-read on
        # fresh dispatch cycles before reporting the outcome.
        index = result["index"]
        expected_id = result.get("target_event_id")
        try:
            observed_count = conn.send_command("native.call", {
                "module": "channels", "method": "channelCount", "args": [True]})
            observed_type = conn.send_command("native.call", {
                "module": "channels", "method": "getChannelType", "args": [index, True]})
            links = conn.send_command("native.readAutomationLinks", {
                "context": context, "channel": index, "offset": 0, "limit": 16})
        except RuntimeError as exc:
            return {**result, "request_id": request_id, "context": context,
                    "error": result.get("error", "") + " Follow-up read failed: " + str(exc)}
        verified = (observed_count.get("success") and observed_count.get("result") == index + 1
                    and links.get("success") and links.get("count") == 1
                    and links.get("links")
                    and links["links"][0].get("target_event_id") == expected_id)
        if verified:
            result = {**result, "success": True, "state": "applied_verified",
                      "verified_on_followup": True,
                      "channel_type": observed_type.get("result"),
                      "links": links.get("links")}
            result.pop("pending_identity", None)
            result.pop("error", None)
        else:
            result = {**result, "request_id": request_id, "context": context,
                      "followup": {
                          "channel_count": observed_count.get("result"),
                          "channel_type": observed_type.get("result")
                              if observed_type.get("success") else observed_type.get("error"),
                          "links_success": links.get("success"),
                          "links_count": links.get("count"),
                          "links_error": links.get("error"),
                      }}
    return {**result, "request_id": request_id, "context": context,
            "submitted": bool(result.get("submitted"))}

def fl_replace_mixer_effect(
    name: str, track: int, slot: int, dry_run: bool = False,
    preset_path: str | None = None,
) -> dict:
    """Replace the effect in an occupied Mixer slot with another preset.

    Mixer track 0 is Master; slot 0 is the first visible effect slot. The
    removal step is NOT undoable on this build, so the whole replacement is
    destructive — take an fl_save_project_copy first. A failure after removal
    is reported as partial with the slot left empty. The result reads the slot
    back independently.
    """
    if type(track) is not int or track < 0 or type(slot) is not int or not 0 <= slot < 10:
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": "Invalid track or slot"}
    if not isinstance(name, str) or not name.strip():
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": "Effect name is required"}
    name = name.strip()
    if type(dry_run) is not bool:
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": "dry_run must be a Boolean"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if (not capabilities.get("success") or not native.get("available")
            or "mixer.remove_effect" not in native.get("operations", [])
            or "mixer.insert_empty_slot" not in native.get("operations", [])):
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": native.get("error")
                or "Mixer effect replacement is unavailable on this bridge"}
    context = capabilities["context"]
    conn = get_connection()
    try:
        host = conn.send_command("mixer.effectLoaderInfo", {}, timeout=5.0)
    except RuntimeError as exc:
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": str(exc)}
    engine_path = host.get("engine_path")
    if not isinstance(engine_path, str):
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": host.get("error") or "The local effect loader is unavailable"}
    try:
        preset = _effect_preset(name, engine_path, preset_path)
    except (OSError, ValueError) as exc:
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": str(exc)}
    read_id = uuid4().hex
    observed = conn.send_command("mixer.readEffectSlot",
                                 {"track": track, "slot": slot, "request_id": read_id})
    if not observed.get("success"):
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": observed.get("error") or "Cannot read the requested slot"}
    if not observed.get("occupied"):
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": "The requested slot is empty; use a direct load instead"}
    removed_name = observed.get("plugin_name")
    if isinstance(removed_name, str) and removed_name.strip().casefold() == name.casefold():
        return {"track": track, "slot": slot, "success": False, "submitted": False,
                "error": "The replacement is already the occupant of this slot"}
    request_id = uuid4().hex
    try:
        result = conn.send_command(
            "native.replaceMixerEffect",
            {"request_id": request_id, "context": context, "track": track, "slot": slot,
             "name": name, "preset_path": str(preset), "dry_run": dry_run,
             "deadline": time.time() + 15},
            timeout=45.0,
        )
    except RuntimeError as exc:
        return {"track": track, "slot": slot, "success": False,
                "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"track": track, "slot": slot, "success": False,
                "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "removed_plugin": removed_name,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return {**result, "request_id": request_id, "context": context,
                "removed_plugin": removed_name}
    verify_id = uuid4().hex
    after = conn.send_command("mixer.readEffectSlot",
                              {"track": track, "slot": slot, "request_id": verify_id})
    verified = (after.get("success") and after.get("context") == context
                and after.get("occupied") is True
                and isinstance(after.get("plugin_name"), str)
                and after["plugin_name"].strip().casefold() == name.casefold())
    merged = {**result, "request_id": request_id, "context": context,
              "removed_plugin": removed_name,
              "verified": bool(verified and result.get("success")),
              "observed": after.get("plugin_name")}
    if not verified or not result.get("success"):
        merged["success"] = False
        if verified is False and not merged.get("error"):
            merged["error"] = "The replacement effect was not confirmed in this slot"
    return merged

def fl_manage_pattern(
    mode: str, index: int | None = None, name: str | None = None,
    color: int | None = None, dry_run: bool = False,
) -> dict:
    """Create, clone, or retitle Patterns through FL's public patterns module.

    mode="create" selects FL's first empty pattern; mode="clone" deep-copies
    pattern `index` into the next free slot; mode="metadata" renames/recolors
    pattern `index` (color is 0xRRGGBB). Pattern deletion has no public
    interface and is not offered. dry_run validates without editing.
    """
    import time

    if mode not in ("create", "clone", "metadata") or type(dry_run) is not bool:
        return {"success": False, "submitted": False, "error": "mode must be create, clone, or metadata"}
    if mode != "create" and (type(index) is not int or index < 0):
        return {"success": False, "submitted": False, "error": "A valid pattern index is required"}
    if mode == "metadata" and name is None and color is None:
        return {"success": False, "submitted": False, "error": "A name or color is required"}
    capabilities = fl_list_capabilities()
    if not capabilities.get("success"):
        return {"success": False, "submitted": False,
                "error": capabilities.get("error") or "FL is not connected"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.managePattern",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "mode": mode, "index": index, "name": name, "color": color, "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    return {**result, "request_id": request_id, "context": context}

def fl_set_pattern_length(index: int, length_steps: int, dry_run: bool = False) -> dict:
    """Change a pattern's length in steps through the public patterns module."""
    import time

    if type(index) is not int or index < 0 or type(length_steps) is not int or not 1 <= length_steps <= 9999:
        return {"success": False, "submitted": False, "error": "Invalid pattern length request"}
    capabilities = fl_list_capabilities()
    if not capabilities.get("success"):
        return {"success": False, "submitted": False,
                "error": capabilities.get("error") or "FL is not connected"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.setPatternLength",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "index": index, "length_steps": length_steps, "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    return {**result, "request_id": request_id, "context": context}

def fl_read_pattern_notes(pattern: int, offset: int = 0, limit: int = 48) -> dict:
    """Read points of a pattern's piano-roll notes (1-based pattern number).

    Each note reports position and length in PPQ ticks, channel, key,
    velocity, pan, release, fine pitch and its raw 24-byte record. This
    requires the Windows native bridge for the verified FL Studio build.
    """
    import time

    if (type(pattern) is not int or not 1 <= pattern <= 999
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 48):
        return {"success": False, "error": "pattern must be 1..999; offset >= 0; limit 1..48"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "error": native.get("error") or capabilities.get("error")}
    if "pattern.read_notes" not in native.get("operations", []):
        return {"success": False, "error": "Note reading is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        response = get_connection().send_command(
            "native.readPatternNotes",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "pattern": pattern, "offset": offset, "limit": limit},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "request_id": request_id, "error": str(exc)}
    if response.get("native_request_id") != request_id:
        return {"success": False, "request_id": request_id,
                "error": response.get("error") or "Controller response could not be matched"}
    return response

def fl_add_pattern_notes(pattern: int, notes: list, dry_run: bool = False) -> dict:
    """Append complete piano-roll notes to a pattern (1-based number).

    Each note is a dict with channel (default 0), key (0..131), position and
    length in PPQ ticks, velocity (0..127, default 100), optional fine_pitch
    and release. One FL undo entry covers the batch. FL must be stopped.
    """
    import time

    if (type(pattern) is not int or not 1 <= pattern <= 999
            or not isinstance(notes, list) or not notes or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid note batch request"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "pattern.add_notes" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Note insertion is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.addPatternNotes",
            {"request_id": request_id, "context": context, "deadline": time.time() + 20,
             "pattern": pattern, "notes": notes, "dry_run": dry_run},
            timeout=25.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_edit_pattern_notes(
    pattern: int,
    edits: list | None = None,
    deletes: list | None = None,
    allow_multiple: bool = False,
    dry_run: bool = False,
) -> dict:
    """Edit or delete existing notes matched by (channel, key, position[, length]).

    edits entries: {target: {channel, key, position, length?}, set: {position?,
    length?, key?, velocity?}}. deletes entries: the same target shape. Notes
    are matched against the live snapshot; a target matching several stacked
    notes is refused unless allow_multiple is set. One FL undo entry covers
    the whole edit. Appending belongs to fl_add_pattern_notes. FL must be
    stopped.
    """
    import struct
    import time

    if (type(pattern) is not int or not 1 <= pattern <= 999
            or type(allow_multiple) is not bool or type(dry_run) is not bool
            or (edits is None) == (deletes is None)):
        return {"success": False, "submitted": False, "error": "Provide edits or deletes"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "pattern.write_notes" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Note editing is unavailable on this bridge"}
    context = capabilities["context"]
    page = fl_read_pattern_notes(pattern, 0, 48)
    if not page.get("success") or page.get("context") != context:
        return {"success": False, "submitted": False,
                "error": page.get("error") or "Cannot read the pattern's notes"}
    records = bytearray()
    notes = list(page.get("notes", []))
    while page.get("next_offset") is not None:
        page = fl_read_pattern_notes(pattern, page["next_offset"], 48)
        if not page.get("success"):
            return {"success": False, "submitted": False,
                    "error": page.get("error") or "Cannot page through the notes"}
        notes.extend(page.get("notes", []))
    for note in notes:
        records += bytes.fromhex(note["record_hex"])
    if not notes:
        return {"success": False, "submitted": False,
                "error": "The pattern has no notes to edit"}

    def match(target):
        if not isinstance(target, dict):
            raise ValueError("Each target is a dict")
        channel = target.get("channel", 0)
        key = target.get("key")
        position = target.get("position")
        length = target.get("length")
        if (type(channel) is not int or type(key) is not int or type(position) is not int
                or (length is not None and type(length) is not int)):
            raise ValueError("Invalid note target")
        hits = [n for n in notes
                if n["channel"] == channel and n["key"] == key
                and n["position"] == position
                and (length is None or n["length"] == length)]
        if len(hits) > 1 and not allow_multiple:
            raise ValueError(
                "The target matches %d stacked notes; narrow it with lengthTick "
                "or pass allow_multiple" % len(hits))
        return hits

    changed = 0
    try:
        for edit in (edits or []):
            if not isinstance(edit, dict):
                raise ValueError("Each edit is a dict")
            hits = match(edit.get("target"))
            changes = edit.get("set") or {}
            position = changes.get("position")
            length = changes.get("length")
            key = changes.get("key")
            velocity = changes.get("velocity")
            for hit in hits:
                raw = bytearray(bytes.fromhex(hit["record_hex"]))
                if position is not None:
                    if type(position) is not int or position < 0:
                        raise ValueError("Invalid position")
                    struct.pack_into("<i", raw, 0, position)
                if length is not None:
                    if type(length) is not int or length < 1:
                        raise ValueError("Invalid length")
                    struct.pack_into("<i", raw, 8, length)
                if key is not None:
                    if type(key) is not int or not 0 <= key <= 131:
                        raise ValueError("Invalid key")
                    struct.pack_into("<H", raw, 12, key)
                if velocity is not None:
                    if type(velocity) is not int or not 0 <= velocity <= 127:
                        raise ValueError("Invalid velocity")
                    raw[21] = velocity
                hit["record_hex"] = bytes(raw).hex()
                changed += 1
        deleted_targets = [hit for d in (deletes or []) for hit in match(d)]
        deleted_ids = {id(hit) for hit in deleted_targets}
        kept = [n for n in notes if id(n) not in deleted_ids]
    except ValueError as exc:
        return {"success": False, "submitted": False, "error": str(exc)}
    kept_records = [bytes.fromhex(note["record_hex"]) for note in kept]
    kept_records.sort(key=lambda raw: struct.unpack_from("<i", raw, 0)[0])
    result_records = bytearray()
    for raw in kept_records:
        result_records += raw
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.writePatternNotes",
            {"request_id": request_id, "context": context, "deadline": time.time() + 20,
             "pattern": pattern, "records_hex": records.hex(),
             "new_records_hex": result_records.hex(), "dry_run": dry_run},
            timeout=25.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success")),
            "changed": changed, "deleted": len(notes) - len(kept),
            "count_before": len(notes), "count_after": len(kept)}

def fl_read_timeline_markers(offset: int = 0, limit: int = 32) -> dict:
    """Read a page of the active arrangement's timeline markers.

    Ticks are absolute PPQ positions; names may be empty. This requires the
    Windows native bridge for the verified FL Studio build.
    """
    import time

    if (type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 32):
        return {"success": False, "error": "offset must be nonnegative and limit 1..32"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "error": native.get("error") or capabilities.get("error")}
    if "timeline.read_markers" not in native.get("operations", []):
        return {"success": False, "error": "Marker reading is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        response = get_connection().send_command(
            "native.readTimelineMarkers",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "offset": offset, "limit": limit},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "request_id": request_id, "error": str(exc)}
    if response.get("native_request_id") != request_id:
        return {"success": False, "request_id": request_id,
                "error": response.get("error") or "Controller response could not be matched"}
    return response

def fl_add_timeline_marker(tick: int, name: str, dry_run: bool = False) -> dict:
    """Add a named timeline marker at an absolute PPQ tick.

    One FL undo entry covers the addition. The engine inserts markers sorted
    by tick. FL must be stopped. dry_run validates without writing.
    """
    import time

    if (type(tick) is not int or tick < 0 or not isinstance(name, str)
            or not name.strip() or len(name) > 200 or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid marker request"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "timeline.add_marker" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Marker insertion is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.addTimelineMarker",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "tick": tick, "name": name.strip(), "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_delete_timeline_marker(index: int, expected_tick: int, dry_run: bool = False) -> dict:
    """Delete the timeline marker at a zero-based index against its observed tick.

    The deletion is refused when the marker changed since the observation. One
    FL undo entry covers it. FL must be stopped. dry_run validates without
    writing.
    """
    import time

    if (type(index) is not int or index < 0 or type(expected_tick) is not int
            or expected_tick < 0 or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid marker deletion"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "timeline.delete_marker" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Marker deletion is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.deleteTimelineMarker",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "index": index, "expected_tick": expected_tick, "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_save_project_copy(output_path: str) -> dict:
    """Write the live project to an absolute .flp path without saving over it.

    Uses the engine's FLP writer worker, bypassing the Save wrapper's modal
    error path. The copy serializes live state including unsaved changes.
    The project itself is not modified. FL must be stopped.
    """
    import time
    from pathlib import Path as _P

    if (not isinstance(output_path, str) or not _P(output_path).is_absolute()
            or _P(output_path).suffix.casefold() != ".flp"):
        return {"success": False, "submitted": False,
                "error": "Use an absolute path ending in .flp"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "project.save_copy" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Project save-copy is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.saveProjectCopy",
            {"request_id": request_id, "context": context,
             "deadline": time.time() + 120, "path": output_path},
            timeout=130.0,
        )
    except RuntimeError as exc:
        return {"success": False, "submitted": True, "write_outcome_unknown": True,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "submitted": True, "write_outcome_unknown": True,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_read_arrangements() -> dict:
    """Read the arrangement list with names and the current arrangement index.

    This requires the Windows native bridge for the verified FL Studio build.
    """
    import time

    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "error": native.get("error") or capabilities.get("error")}
    if "arrangement.read_all" not in native.get("operations", []):
        return {"success": False, "error": "Arrangement reading is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        response = get_connection().send_command(
            "native.readArrangements",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "request_id": request_id, "error": str(exc)}
    if response.get("native_request_id") != request_id:
        return {"success": False, "request_id": request_id,
                "error": response.get("error") or "Controller response could not be matched"}
    return response

def fl_add_arrangement(name: str = "", switch_to: bool = True, dry_run: bool = False) -> dict:
    """Add a new empty arrangement, optionally switching to it.

    One FL undo entry covers the addition. FL must be stopped.
    """
    import time

    if not isinstance(name, str) or len(name) > 200 or type(switch_to) is not bool or type(dry_run) is not bool:
        return {"success": False, "submitted": False, "error": "Invalid arrangement request"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "arrangement.add" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Arrangement insertion is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.addArrangement",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "name": name, "switch_to": switch_to, "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_clone_arrangement(
    source: int, name: str = "", switch_to: bool = True, dry_run: bool = False,
) -> dict:
    """Deep-copy an arrangement (clips and markers) into a new arrangement.

    One FL undo entry covers the clone. FL must be stopped.
    """
    import time

    if (type(source) is not int or source < 0 or not isinstance(name, str)
            or len(name) > 200 or type(switch_to) is not bool or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid clone request"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "arrangement.clone" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Arrangement cloning is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.cloneArrangement",
            {"request_id": request_id, "context": context, "deadline": time.time() + 20,
             "source": source, "name": name, "switch_to": switch_to, "dry_run": dry_run},
            timeout=25.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_delete_arrangement(index: int, dry_run: bool = False) -> dict:
    """Delete an arrangement (never the last remaining one).

    One FL undo entry covers the deletion. FL must be stopped. dry_run
    validates without writing.
    """
    import time

    if type(index) is not int or index < 0 or type(dry_run) is not bool:
        return {"success": False, "submitted": False, "error": "Invalid deletion request"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "arrangement.delete" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Arrangement deletion is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.deleteArrangement",
            {"request_id": request_id, "context": context, "deadline": time.time() + 20,
             "index": index, "dry_run": dry_run},
            timeout=25.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_set_current_arrangement(index: int, dry_run: bool = False) -> dict:
    """Switch the visible arrangement and confirm the engine agrees.

    This is a view change, not a project edit, so no undo entry is created.
    FL must be stopped. dry_run reports the current index without switching.
    """
    import time

    if type(index) is not int or index < 0 or type(dry_run) is not bool:
        return {"success": False, "submitted": False, "error": "Invalid index or dry_run"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or "Native bridge unavailable"}
    if "arrangement.set_current" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Arrangement switching is unavailable on this bridge"}
    request_id = uuid4().hex
    try:
        response = get_connection().send_command("native.setCurrentArrangement", {
            "request_id": request_id, "context": capabilities["context"],
            "index": index, "dry_run": dry_run, "deadline": time.time() + 10,
        })
    except RuntimeError as exc:
        return {"success": False, "submitted": False, "error": str(exc)}
    if response.get("native_request_id") != request_id:
        return {"success": False, "submitted": False,
                "error": response.get("error") or "Response was not matched"}
    return response


def fl_set_arrangement_name(index: int, name: str, dry_run: bool = False) -> dict:
    """Rename an arrangement with one FL undo entry."""
    import time

    if (type(index) is not int or index < 0 or not isinstance(name, str)
            or len(name) > 200 or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid rename request"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or capabilities.get("error")}
    if "arrangement.set_name" not in native.get("operations", []):
        return {"success": False, "submitted": False,
                "error": "Arrangement renaming is unavailable on this bridge"}
    context = capabilities["context"]
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.setArrangementName",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "index": index, "name": name, "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_move_playlist_clip(
    snapshot_ref: str, start_tick: int, track: int | None = None, dry_run: bool = False,
) -> dict:
    """Move a clip identified by a fresh fl_list_playlist_clips snapshot.

    The target start is an absolute PPQ tick. Track is one-based and defaults
    to the current track. FL must be stopped. Set dry_run to preview without editing.
    The result is read back separately.
    """
    import struct
    import time

    try:
        context, revision_text, arrangement_text, index_text, record_hex = (
            snapshot_ref.rsplit(":", 4)
        )
        if not revision_text.startswith("p") or int(revision_text[1:]) < 0:
            raise ValueError("invalid Playlist revision")
        arrangement, index = int(arrangement_text), int(index_text)
        record = bytes.fromhex(record_hex)
        if len(record) != 36:
            raise ValueError("invalid record length")
        track_raw = struct.unpack_from("<h", record, 12)[0]
    except (AttributeError, TypeError, ValueError, struct.error):
        return {"success": False, "submitted": False, "error": "Invalid clip snapshot reference"}
    if type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF:
        return {"success": False, "submitted": False, "error": "Invalid start tick"}
    if type(dry_run) is not bool:
        return {"success": False, "submitted": False, "error": "dry_run must be a Boolean"}
    target_track = 500 - track_raw if track is None else track
    if type(target_track) is not int or not 1 <= target_track <= 500:
        return {"success": False, "submitted": False, "error": "Invalid Playlist track"}
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.movePlaylistClip",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "snapshot_ref": snapshot_ref, "start_tick": start_tick, "track": target_track,
             "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    observed = fl_list_playlist_clips(offset=index, limit=1)
    clips = observed.get("clips", [])
    clip = clips[0] if len(clips) == 1 else {}
    expected_record = bytearray(record)
    struct.pack_into("<i", expected_record, 0, start_tick)
    struct.pack_into("<h", expected_record, 12, 500 - target_track)
    observed_snapshot = clip.get("snapshot_ref", "")
    try:
        observed_context, observed_revision, observed_arrangement, observed_index, observed_hex = (
            observed_snapshot.rsplit(":", 4)
        )
    except (AttributeError, ValueError):
        observed_context, observed_revision, observed_arrangement, observed_index, observed_hex = (
            None, None, None, None, None
        )
    verified = (
        observed.get("success") and observed.get("context") == context
        and observed.get("arrangement") == arrangement and clip.get("index") == index
        and clip.get("start_tick") == start_tick and clip.get("track") == target_track
        and observed_context == context and observed_revision.startswith("p")
        and observed_arrangement == str(arrangement) and observed_index == str(index)
        and observed_hex == expected_record.hex()
    )
    return {
        "success": bool(verified and result.get("success")),
        "submitted": True,
        "verified": bool(verified),
        "request_id": request_id,
        "context": context,
        "observed": clip or None,
        **({"error": result.get("error") or "Playlist move could not be verified"}
           if not verified or not result.get("success") else {}),
    }

def _parse_snapshot_reference(snapshot_ref: str):
    """Validate a clip snapshot reference shape; return (context, request_id) or an error dict."""
    if not isinstance(snapshot_ref, str) or snapshot_ref.count(":") < 4:
        return {"success": False, "submitted": False, "error": "Invalid clip snapshot reference"}
    context = snapshot_ref.rsplit(":", 4)[0]
    try:
        revision_text, arrangement_text, index_text, record_hex = snapshot_ref.rsplit(":", 4)[1:]
        if not revision_text.startswith("p") or int(revision_text[1:]) < 0:
            raise ValueError("invalid revision")
        int(arrangement_text), int(index_text)
        record = bytes.fromhex(record_hex)
        if len(record) != 36:
            raise ValueError("invalid record length")
    except (AttributeError, TypeError, ValueError, struct.error):
        return {"success": False, "submitted": False, "error": "Invalid clip snapshot reference"}
    return context, uuid4().hex

def fl_delete_playlist_clip(snapshot_ref: str, dry_run: bool = False) -> dict:
    """Delete a clip identified by a fresh fl_list_playlist_clips snapshot.

    FL compacts the remaining clips, so indices after the deleted clip shift.
    FL must be stopped. Set dry_run to preview without editing.
    """
    import time

    parsed = _parse_snapshot_reference(snapshot_ref)
    if isinstance(parsed, dict):
        return parsed
    context, request_id = parsed
    try:
        result = get_connection().send_command(
            "native.deletePlaylistClip",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "snapshot_ref": snapshot_ref, "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_duplicate_playlist_clip(
    snapshot_ref: str, start_tick: int | None = None, track: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Duplicate a clip identified by a fresh fl_list_playlist_clips snapshot.

    The duplicate keeps the source clip's content, source window, and mute
    state, and receives a new start tick (absolute PPQ) and one-based track.
    Either may be omitted to keep the source value. FL must be stopped.
    """
    import struct
    import time

    parsed = _parse_snapshot_reference(snapshot_ref)
    if isinstance(parsed, dict):
        return parsed
    context, request_id = parsed
    record = bytes.fromhex(snapshot_ref.rsplit(":", 1)[1])
    if start_tick is None:
        start_tick = struct.unpack_from("<i", record)[0]
    if track is None:
        track = 500 - struct.unpack_from("<h", record, 12)[0]
    if type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF:
        return {"success": False, "submitted": False, "error": "Invalid start tick"}
    if type(track) is not int or not 1 <= track <= 500:
        return {"success": False, "submitted": False, "error": "Invalid Playlist track"}
    try:
        result = get_connection().send_command(
            "native.duplicatePlaylistClip",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "snapshot_ref": snapshot_ref, "start_tick": start_tick, "track": track,
             "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_trim_playlist_clip(
    snapshot_ref: str, window_start_tick: int, window_end_tick: int, dry_run: bool = False,
) -> dict:
    """Trim a clip identified by a fresh fl_list_playlist_clips snapshot.

    For audio clips the window is absolute source ticks; the engine converts
    it to song milliseconds and sizes the clip to the window. For pattern
    clips the clip length becomes window_end_tick - window_start_tick.
    FL must be stopped. Set dry_run to preview without editing.
    """
    import time

    parsed = _parse_snapshot_reference(snapshot_ref)
    if isinstance(parsed, dict):
        return parsed
    context, request_id = parsed
    if (type(window_start_tick) is not int or window_start_tick < 0
            or type(window_end_tick) is not int or window_end_tick <= window_start_tick
            or window_end_tick > 0x7FFFFFFF or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid trim window"}
    try:
        result = get_connection().send_command(
            "native.trimPlaylistClip",
            {"request_id": request_id, "context": context, "deadline": time.time() + 15,
             "snapshot_ref": snapshot_ref, "window_start_tick": window_start_tick,
             "window_end_tick": window_end_tick, "dry_run": dry_run},
            timeout=20.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_split_playlist_clip(snapshot_ref: str, split_tick: int, dry_run: bool = False) -> dict:
    """Split a clip identified by a fresh fl_list_playlist_clips snapshot.

    split_tick is an absolute PPQ tick inside the clip. The result is the
    left half at the original start and the right half at split_tick, both on
    the original track, with the audio source window divided accordingly. One
    FL undo entry covers the whole split; partial failures list the completed
    steps. FL must be stopped. Set dry_run to preview without editing.
    """
    import time

    parsed = _parse_snapshot_reference(snapshot_ref)
    if isinstance(parsed, dict):
        return parsed
    context, request_id = parsed
    if type(split_tick) is not int or not 0 <= split_tick <= 0x7FFFFFFF or type(dry_run) is not bool:
        return {"success": False, "submitted": False, "error": "Invalid split tick"}
    try:
        result = get_connection().send_command(
            "native.splitPlaylistClip",
            {"request_id": request_id, "context": context, "deadline": time.time() + 30,
             "snapshot_ref": snapshot_ref, "split_tick": split_tick, "dry_run": dry_run},
            timeout=35.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_set_playlist_clip_muted(snapshot_ref: str, muted: bool, dry_run: bool = False) -> dict:
    """Mute or unmute a clip identified by a fresh fl_list_playlist_clips snapshot.

    The mute flag persists in saved projects. FL must be stopped. Set dry_run
    to preview without editing.
    """
    import time

    if type(muted) is not bool:
        return {"success": False, "submitted": False, "error": "muted must be a Boolean"}
    parsed = _parse_snapshot_reference(snapshot_ref)
    if isinstance(parsed, dict):
        return parsed
    context, request_id = parsed
    try:
        result = get_connection().send_command(
            "native.setPlaylistClipMuted",
            {"request_id": request_id, "context": context, "deadline": time.time() + 10,
             "snapshot_ref": snapshot_ref, "muted": muted, "dry_run": dry_run},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    return {**result, "request_id": request_id, "context": context,
            "submitted": True, "success": bool(result.get("success"))}

def fl_list_automation_points(channel: int, offset: int = 0, limit: int = 32) -> dict:
    """Read points of an Automation Clip by global Channel Rack index.

    Point time is in beats and values are normalized. Audio and ordinary
    instrument channels are rejected. This operation does not change the curve.
    """
    import time

    if type(channel) is not int or channel < 0 or type(offset) is not int or offset < 0:
        return {"success": False, "error": "channel and offset must be nonnegative integers"}
    if type(limit) is not int or not 1 <= limit <= 64:
        return {"success": False, "error": "limit must be 1..64"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "error": native.get("error") or capabilities.get("error")}
    if "automation.read_points" not in native.get("operations", []):
        return {"success": False, "error": "Automation point reading is unavailable"}
    request_id = uuid4().hex
    try:
        response = get_connection().send_command(
            "native.readAutomationPoints",
            {"request_id": request_id, "context": capabilities["context"],
             "deadline": time.time() + 10, "channel": channel, "offset": offset, "limit": limit},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "request_id": request_id, "error": str(exc)}
    if response.get("native_request_id") != request_id:
        return {"success": False, "request_id": request_id,
                "error": response.get("error") or "Controller response could not be matched"}
    return response


_SHAPE_FUNCTIONS = {
    "ease_in": lambda t: t * t,
    "ease_out": lambda t: 1.0 - (1.0 - t) * (1.0 - t),
    "ease_in_out": lambda t: 2 * t * t if t < 0.5 else 1.0 - 2 * (1.0 - t) * (1.0 - t),
    "exponential": lambda t: (2.0 ** (10 * t) - 1.0) / 1023.0,
}

def fl_shape_automation_segment(
    channel: int, start_index: int, shape: str = "ease_in", count: int = 16,
    dry_run: bool = False,
) -> dict:
    """Shape an automation segment by inserting interior linear points.

    A segment runs from point ``start_index`` to point ``start_index + 1``.
    This tool inserts ``count`` interior points whose values follow the chosen
    shape function while every point itself stays linear (tension=0, curve=0) —
    on this FL build the curve dword is stored but the engine renders from a
    zeroed derived cache, so a dense set of linear points is the reliable way
    to obtain an eased ramp. Requires a stopped transport and a stable
    controller context. Each inserted point is its own undo step (``count``
    undos restore the original two-point segment). The result reports the
    endpoint values and the sampled shape so the change can be audited.
    """
    import math

    if (type(channel) is not int or channel < 0 or type(start_index) is not int
            or start_index < 0 or type(count) is not int or not 8 <= count <= 62
            or shape not in _SHAPE_FUNCTIONS or type(dry_run) is not bool):
        return {"success": False, "submitted": False,
                "error": "Invalid channel, index, shape, count (8..62) or dry_run"}
    page = fl_list_automation_points(channel, 0, 64)
    if not page.get("success"):
        return {"success": False, "submitted": False,
                "error": page.get("error") or "Cannot read automation points"}
    points = page["points"]
    if start_index + 1 >= len(points):
        return {"success": False, "submitted": False,
                "error": "Segment endpoint does not exist"}
    begin, end = points[start_index], points[start_index + 1]
    t0, t1 = begin["time_beats"], end["time_beats"]
    v0, v1 = begin["value"], end["value"]
    if (not isinstance(t0, (int, float)) or not isinstance(t1, (int, float))
            or t1 <= t0 or not isinstance(v0, (int, float))
            or not isinstance(v1, (int, float)) or not math.isfinite(t1 - t0)):
        return {"success": False, "submitted": False, "error": "Invalid segment geometry"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "submitted": False,
                "error": native.get("error") or "The native bridge is unavailable"}
    context = capabilities["context"]
    fn = _SHAPE_FUNCTIONS[shape]
    planned = [
        {"index": start_index + i, "time_beats": t0 + (t1 - t0) * i / count,
         "value": v0 + (v1 - v0) * fn(i / count), "tension": 0.0}
        for i in range(1, count)
    ]
    result = {"success": True, "submitted": False, "dry_run": dry_run,
              "channel": channel, "start_index": start_index, "shape": shape,
              "count": count, "begin": {"time_beats": t0, "value": v0},
              "end": {"time_beats": t1, "value": v1}, "planned": planned}
    if dry_run:
        return result
    inserted = 0
    errors = []
    for point in planned:
        fresh = fl_list_automation_points(channel, 0, 64)
        if not fresh.get("success"):
            errors.append("snapshot refresh failed")
            break
        _ctx, _ch, source, _idx, _prior, raw = (
            fresh["points"][start_index]["snapshot_ref"].rsplit(":", 5)
        )
        records = "".join(pt["snapshot_ref"].rsplit(":", 1)[1] for pt in fresh["points"])
        request_id = uuid4().hex
        try:
            response = get_connection().send_command("native.editAutomationPoints", {
                "request_id": request_id, "context": context, "channel": channel,
                "source": int(source), "index": point["index"], "mode": "insert",
                "time_beats": point["time_beats"], "value": point["value"],
                "tension": point["tension"], "records_hex": records, "dry_run": False,
            }, timeout=30.0)
        except RuntimeError as exc:
            errors.append(str(exc))
            break
        if response.get("native_request_id") != request_id or not response.get("success"):
            errors.append(response.get("error") or "Point insert was not confirmed")
            break
        inserted += 1
    result["submitted"] = inserted > 0
    result["inserted"] = inserted
    result["undo_steps"] = inserted
    result["success"] = inserted == count - 1
    if errors:
        result["error"] = errors[0]
    return result


def fl_list_automation_links(
    channel: int, offset: int = 0, limit: int = 32, include_names: bool = False,
    include_values: bool = False, include_targets: bool = False,
) -> dict:
    """Read target event IDs for an Automation Clip channel, including unplaced clips.

    Event IDs are raw FL host identifiers. Set include_names to request FL's
    event display names. Set include_values to sample the host's current
    normalized target value; unavailable values are null. include_targets
    resolves verified Mixer effect parameters by current location (limit <= 16).
    Unresolved targets are null. An unavailable registry is an error, distinct
    from a registry containing zero links.
    """
    if (type(channel) is not int or channel < 0 or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 64 or type(include_names) is not bool
            or type(include_values) is not bool or type(include_targets) is not bool
            or (include_targets and limit > 16)):
        return {"success": False, "error": "Invalid automation channel or link page"}
    capabilities = fl_list_capabilities()
    native = capabilities.get("native", {})
    if not capabilities.get("success") or not native.get("available"):
        return {"success": False, "error": native.get("error") or capabilities.get("error")}
    if "automation.read_links" not in native.get("operations", []):
        return {"success": False, "error": "Automation link reading is unavailable"}
    request_id = uuid4().hex
    try:
        response = get_connection().send_command(
            "native.readAutomationLinks",
            {"request_id": request_id, "context": capabilities["context"],
             "deadline": time.time() + 10, "channel": channel, "offset": offset, "limit": limit,
             "include_names": include_names, "include_values": include_values,
             "include_targets": include_targets},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "request_id": request_id, "error": str(exc)}
    if response.get("native_request_id") != request_id:
        return {"success": False, "request_id": request_id,
                "error": response.get("error") or "Controller response could not be matched"}
    if (include_values and response.get("success") and
            any("target_value" not in link for link in response.get("links", []))):
        return {"success": False, "request_id": request_id,
                "error": "Reload the updated controller to read automation target values"}
    if (include_targets and response.get("success") and
            any("target" not in link for link in response.get("links", []))):
        return {"success": False, "request_id": request_id,
                "error": "Reload the updated controller to resolve automation targets"}
    return response


def fl_set_automation_point_value(snapshot_ref: str, value: float, dry_run: bool = False) -> dict:
    """Set a previously observed Automation Clip point to a normalized value.

    Obtain snapshot_ref from fl_list_automation_points. This edits only the value;
    point time, curve and tension are not changed. FL must be stopped. Use
    dry_run to validate the request without creating an undo entry or changing it.
    """
    return _set_automation_point_float(snapshot_ref, value, dry_run, "value")


def fl_set_automation_point_tension(
    snapshot_ref: str, tension: float, dry_run: bool = False,
) -> dict:
    """Set a previously observed Automation Clip point's tension in -1..1.

    Obtain snapshot_ref from fl_list_automation_points. The written float is
    read back independently; the audible curve remains dependent on its mode.
    """
    return _set_automation_point_float(snapshot_ref, tension, dry_run, "tension")


def fl_set_automation_point_curve(snapshot_ref: str, curve: int, dry_run: bool = False) -> dict:
    """Set a previously observed Automation Clip point's curve-shape dword.

    Obtain snapshot_ref from fl_list_automation_points. The dword sits at the
    point record's bytes +0x0C..+0x0F, right after the tension float, and
    selects how FL renders the segment from this point to the next one. Only
    values verified against the actual host output are accepted (0 = linear);
    the accepted set grows as each shape is measured. The written dword is
    read back independently.
    """
    import math
    import struct

    try:
        context, channel_text, source_text, index_text, prior_time, record_hex = (
            snapshot_ref.rsplit(":", 5)
        )
        channel, source, index = int(channel_text), int(source_text), int(index_text)
        if not math.isfinite(float.fromhex(prior_time)) or float.fromhex(prior_time) < 0:
            raise ValueError("invalid point time")
        record = bytes.fromhex(record_hex)
        if channel < 0 or source < 0 or index < 0 or len(record) != 32:
            raise ValueError("invalid point")
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {"success": False, "submitted": False, "error": "Invalid automation point snapshot"}
    if (type(curve) is not int or not 0 <= curve <= 0x7FFFFFFF or type(dry_run) is not bool):
        return {"success": False, "submitted": False,
                "error": "curve must be an integer in 0..0x7FFFFFFF; dry_run must be Boolean"}
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.setAutomationPointCurve",
            {"request_id": request_id, "context": context, "snapshot_ref": snapshot_ref,
             "curve": curve, "dry_run": dry_run, "deadline": time.time() + 10},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context,
                "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    observed = fl_list_automation_points(channel=channel, offset=index, limit=1)
    points = observed.get("points", [])
    point = points[0] if len(points) == 1 else {}
    try:
        observed_prefix = bytes.fromhex(point.get("snapshot_ref", "").rsplit(":", 1)[1])[:16]
    except (IndexError, TypeError, ValueError):
        observed_prefix = b""
    expected_prefix = bytearray(record)[:12] + struct.pack("<I", curve)
    verified = (observed.get("success") and observed.get("context") == context
                and observed.get("source_event") == source and point.get("index") == index
                and point.get("curve") == curve
                and observed_prefix == bytes(expected_prefix))
    return {"success": bool(verified and result.get("success")), "submitted": True,
            "verified": bool(verified), "request_id": request_id, "context": context,
            "observed": point or None,
            **({"error": result.get("error") or "Automation point curve could not be verified"}
               if not verified or not result.get("success") else {})}


def _binding_for_source(context: str, source_event: int) -> dict | None:
    """Best-effort binding description for an edited Automation Clip.

    Returns None when the controller cannot corroborate a binding; the edit
    itself is still valid, so a missing binding is never an error.
    """
    try:
        response = get_connection().send_command(
            "native.readAutomationLinks", {
                "request_id": uuid4().hex, "context": context, "channel": 0,
                "offset": 0, "limit": 1, "include_targets": True,
                "source_event": source_event,
            }, timeout=5.0)
    except (OSError, ValueError, RuntimeError):
        return None
    if not response.get("success"):
        return None
    for link in response.get("links", []):
        if link.get("source_event") == source_event and isinstance(link.get("target"), dict):
            return link["target"]
    return None


def _set_automation_point_float(snapshot_ref: str, value: float, dry_run: bool, field: str) -> dict:
    import math
    import struct

    try:
        context, channel_text, source_text, index_text, prior_time, record_hex = (
            snapshot_ref.rsplit(":", 5)
        )
        channel, source, index = int(channel_text), int(source_text), int(index_text)
        if not math.isfinite(float.fromhex(prior_time)) or float.fromhex(prior_time) < 0:
            raise ValueError("invalid point time")
        record = bytes.fromhex(record_hex)
        if channel < 0 or source < 0 or index < 0 or len(record) != 32:
            raise ValueError("invalid point")
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {"success": False, "submitted": False, "error": "Invalid automation point snapshot"}
    lower = -1 if field == "tension" else 0
    if (type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= 1
            or type(dry_run) is not bool):
        return {"success": False, "submitted": False,
                "error": f"{field} must be finite and in {lower}..1; dry_run must be Boolean"}
    request_id = uuid4().hex
    action = ("native.setAutomationPointTension" if field == "tension"
              else "native.setAutomationPointValue")
    try:
        result = get_connection().send_command(
            action,
            {"request_id": request_id, "context": context, "snapshot_ref": snapshot_ref,
             field: value, "dry_run": dry_run, "deadline": time.time() + 10},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    observed = fl_list_automation_points(channel=channel, offset=index, limit=1)
    points = observed.get("points", [])
    point = points[0] if len(points) == 1 else {}
    expected = bytearray(record)
    struct.pack_into("<f", expected, 8 if field == "tension" else 4, value)
    expected_prefix = expected[:16]
    try:
        observed_prefix = bytes.fromhex(point.get("snapshot_ref", "").rsplit(":", 1)[1])[:16]
    except (IndexError, TypeError, ValueError):
        observed_prefix = b""
    verified = (observed.get("success") and observed.get("context") == context
                and observed.get("source_event") == source and point.get("index") == index
                and observed_prefix == expected_prefix)
    return {"success": bool(verified and result.get("success")), "submitted": True,
            "verified": bool(verified), "request_id": request_id, "context": context,
            # Explicit request value, independent readback, and the binding this
            # point belongs to when the controller can corroborate one.
            "requested_value": value,
            "observed": point or None,
            "binding": _binding_for_source(context, source),
            **({"error": result.get("error") or "Automation point edit could not be verified"}
               if not verified or not result.get("success") else {})}

def fl_move_automation_point(snapshot_ref: str, time_beats: float, dry_run: bool = False) -> dict:
    """Move one Automation Clip point, preserving the next point's absolute time.

    Use a fresh fl_list_automation_points snapshot. Positions are in beats;
    moves crossing adjacent points or inexactly representable by FL are rejected.
    """
    import math
    import struct

    try:
        context, ch, src, idx, prior_hex, raw = snapshot_ref.rsplit(":", 5)
        channel, source, index = int(ch), int(src), int(idx)
        prior, record = float.fromhex(prior_hex), bytes.fromhex(raw)
        if (not math.isfinite(prior) or prior < 0 or channel < 0 or source < 0
                or index < 0 or len(record) != 32):
            raise ValueError("invalid point")
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {"success": False, "submitted": False, "error": "Invalid automation snapshot"}
    if (type(time_beats) not in (int, float) or not math.isfinite(time_beats) or time_beats < 0
            or type(dry_run) is not bool):
        return {"success": False, "submitted": False, "error": "Invalid point time or dry_run"}
    if index == 0 and time_beats != 0:
        return {"success": False, "submitted": False,
                "error": "The first automation point must remain at beat zero"}
    before = fl_list_automation_points(channel=channel, offset=index, limit=2)
    points = before.get("points", [])
    if (not before.get("success") or before.get("context") != context or not points
            or points[0].get("snapshot_ref") != snapshot_ref):
        return {"success": False, "submitted": False, "error": "Point changed; read it again"}
    neighbor = points[1] if len(points) > 1 else None
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.moveAutomationPoint",
            {"request_id": request_id, "context": context, "snapshot_ref": snapshot_ref,
             "next_snapshot_ref": neighbor["snapshot_ref"] if neighbor else None,
             "time_beats": time_beats, "dry_run": dry_run, "deadline": time.time() + 10},
            timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    observed = fl_list_automation_points(channel=channel, offset=index, limit=2)
    after = observed.get("points", [])
    expected_record = bytearray(record)
    struct.pack_into("<f", expected_record, 0, time_beats - prior)
    try:
        target_prefix = bytes.fromhex(after[0]["snapshot_ref"].rsplit(":", 1)[1])[:16]
    except (IndexError, KeyError, TypeError, ValueError):
        target_prefix = b""
    verified = (observed.get("success") and observed.get("context") == context
                and observed.get("count") == before.get("count") and len(after) == len(points)
                and observed.get("source_event") == source and after[0].get("index") == index
                and target_prefix == expected_record[:16]
                and after[0].get("time_beats") == time_beats)
    if verified and neighbor:
        old_ref = neighbor["snapshot_ref"]
        next_bytes = bytearray.fromhex(old_ref.rsplit(":", 1)[1])
        struct.pack_into("<f", next_bytes, 0, neighbor["time_beats"] - time_beats)
        try:
            next_prefix = bytes.fromhex(after[1]["snapshot_ref"].rsplit(":", 1)[1])[:16]
        except (IndexError, KeyError, TypeError, ValueError):
            next_prefix = b""
        verified = (after[1].get("index") == index + 1 and next_prefix == next_bytes[:16]
                    and after[1].get("time_beats") == neighbor["time_beats"])
    return {"success": bool(verified and result.get("success")), "submitted": True,
            "verified": bool(verified), "request_id": request_id, "context": context,
            "observed": after, **({"error": result.get("error") or "Move not verified"}
                                  if not verified or not result.get("success") else {})}


def _automation_curve_snapshot(channel: int) -> dict:
    points = []
    context = source = count = None
    offset = 0
    while True:
        page = fl_list_automation_points(channel=channel, offset=offset, limit=64)
        if not page.get("success"):
            return {"success": False, "error": page.get("error")}
        if context is None:
            context, source = page.get("context"), page.get("source_event")
            count = page.get("count")
        if (page.get("context") != context or page.get("source_event") != source
                or page.get("count") != count or type(count) is not int or not 2 <= count <= 4000):
            return {"success": False, "error": "Automation curve changed during enumeration"}
        points.extend(page.get("points", []))
        next_offset = page.get("next_offset")
        if next_offset is None:
            if len(points) != count:
                return {"success": False, "error": "Incomplete automation curve"}
            return {"success": True, "context": context, "source": source, "points": points}
        if type(next_offset) is not int or next_offset != len(points) or next_offset <= offset:
            return {"success": False, "error": "Invalid automation curve pagination"}
        offset = next_offset


def fl_add_automation_point(
    channel: int, time_beats: float, value: float, tension: float = 0, dry_run: bool = False,
) -> dict:
    """Insert a linear Automation Clip point strictly between existing points."""
    return _edit_automation_points(channel, "insert", time_beats, value, tension, None, dry_run)


def fl_delete_automation_point(snapshot_ref: str, dry_run: bool = False) -> dict:
    """Delete an observed interior Automation Clip point, keeping both endpoints."""
    try:
        _, channel, _, _, _, _ = snapshot_ref.rsplit(":", 5)
        channel = int(channel)
    except (AttributeError, TypeError, ValueError):
        return {"success": False, "submitted": False, "error": "Invalid automation snapshot"}
    return _edit_automation_points(channel, "delete", 0, 0, 0, snapshot_ref, dry_run)


def _edit_automation_points(channel, mode, position, value, tension, snapshot_ref, dry_run):
    import math
    import struct

    if (type(channel) is not int or channel < 0 or type(dry_run) is not bool
            or any(type(v) not in (int, float) or not math.isfinite(v)
                   for v in (position, value, tension))
            or position < 0 or not 0 <= value <= 1 or not -1 <= tension <= 1):
        return {"success": False, "submitted": False, "error": "Invalid automation point edit"}
    before = _automation_curve_snapshot(channel)
    if not before.get("success"):
        return {**before, "submitted": False}
    points, context, source = before["points"], before["context"], before["source"]
    if any(p.get("curve") != 0 for p in points):
        return {"success": False, "submitted": False,
                "error": "Insert/delete is only verified for linear curves"}
    if mode == "delete":
        indexes = [i for i, p in enumerate(points) if p.get("snapshot_ref") == snapshot_ref]
        if len(indexes) != 1 or indexes[0] in (0, len(points) - 1):
            return {"success": False, "submitted": False,
                    "error": "Point changed or is a protected endpoint"}
        index = indexes[0]
    else:
        indexes = [i for i in range(1, len(points))
                   if points[i - 1]["time_beats"] < position < points[i]["time_beats"]]
        if len(indexes) != 1:
            return {"success": False, "submitted": False,
                    "error": "Insert strictly between existing points"}
        index = indexes[0]
    try:
        records = b"".join(bytes.fromhex(p["snapshot_ref"].rsplit(":", 1)[1]) for p in points)
        if len(records) != 32 * len(points):
            raise ValueError("Invalid record length")
    except (KeyError, AttributeError, TypeError, ValueError):
        return {"success": False, "submitted": False, "error": "Invalid curve snapshot"}
    request_id = uuid4().hex
    try:
        result = get_connection().send_command(
            "native.editAutomationPoints",
            {"request_id": request_id, "context": context, "channel": channel, "source": source,
             "index": index, "mode": mode, "time_beats": position, "value": value,
             "tension": tension, "records_hex": records.hex(), "dry_run": dry_run,
             "deadline": time.time() + 10}, timeout=15.0,
        )
    except RuntimeError as exc:
        return {"success": False, "write_outcome_unknown": not dry_run,
                "request_id": request_id, "context": context, "error": str(exc)}
    if result.get("native_request_id") != request_id:
        return {"success": False, "write_outcome_unknown": not dry_run, "request_id": request_id,
                "context": context, "error": result.get("error") or "Response was not matched"}
    if not result.get("submitted"):
        return result
    after = _automation_curve_snapshot(channel)
    expected = [{k: p[k] for k in ("time_beats", "value", "tension", "curve")} for p in points]
    if mode == "delete":
        expected.pop(index)
    else:
        f32 = lambda v: struct.unpack("<f", struct.pack("<f", v))[0]  # noqa: E731
        expected.insert(index, {"time_beats": position, "value": f32(value),
                                "tension": f32(tension), "curve": 0})
    actual = [{k: p.get(k) for k in ("time_beats", "value", "tension", "curve")}
              for p in after.get("points", [])]
    verified = (after.get("success") and after.get("context") == context
                and after.get("source") == source and actual == expected)
    return {"success": bool(verified and result.get("success")), "submitted": True,
            "verified": bool(verified), "request_id": request_id, "context": context,
            "points": after.get("points"),
            **({"error": result.get("error") or "Curve edit could not be verified"}
               if not verified or not result.get("success") else {})}


def register_semantic_native_tools(mcp):
    for tool in (fl_list_capabilities, fl_add_sampler, fl_add_generator,
                 fl_read_sampler_sources, fl_replace_sampler_sample,
                 fl_read_mixer_effect_state, fl_clone_mixer_effect_full,
                 fl_list_plugin_presets, fl_load_plugin_preset,
                 fl_describe_plugin_adapter,
                 fl_swap_mixer_effects,
                 fl_shape_automation_segment,
                 fl_list_playlist_clips,
                 fl_insert_audio_clip, fl_insert_pattern_clip, fl_insert_automation_clip,
                 fl_create_automation_source, fl_save_project_copy,
                 fl_replace_mixer_effect,
                 fl_read_arrangements, fl_add_arrangement, fl_set_current_arrangement, fl_clone_arrangement,
                 fl_delete_arrangement, fl_set_arrangement_name,
                 fl_read_timeline_markers, fl_add_timeline_marker, fl_delete_timeline_marker,
                 fl_read_pattern_notes, fl_add_pattern_notes, fl_edit_pattern_notes,
                 fl_manage_pattern, fl_set_pattern_length, fl_move_playlist_clip,
                 fl_delete_playlist_clip, fl_duplicate_playlist_clip, fl_set_playlist_clip_muted,
                 fl_trim_playlist_clip, fl_split_playlist_clip,
                 fl_list_automation_points, fl_list_automation_links, fl_set_automation_point_value,
                 fl_set_automation_point_tension, fl_set_automation_point_curve,
                 fl_move_automation_point,
                 fl_add_automation_point, fl_delete_automation_point):
        mcp.tool()(tool)

    @mcp.tool()
    def fl_load_effect_at_slot(
        name: str, track: int, slot: int, dry_run: bool = False, preset_path: str | None = None,
    ) -> dict:
        """Load an effect into an empty Mixer slot and independently read it back.

        Mixer track 0 is Master; slot 0 is the first visible effect slot.
        """
        try:
            return load_effect_at_slot(get_connection(), name, track, slot, dry_run, preset_path)
        except RuntimeError as exc:
            return {"success": False, "error": str(exc), "write_outcome_unknown": not dry_run}
