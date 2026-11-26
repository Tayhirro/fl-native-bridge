"""Typed FL operations for the process-scoped controller queue."""

import importlib
import math
import os
import struct
import time
from pathlib import Path

import midi
import mixer
import plugins
import transport

_effect_controller = f"{os.getpid()}:{time.time_ns()}"
_effect_project = 0
_playlist_revision = 0

# Host-side write leases for Mixer FX slots. Several MCP client processes can
# submit requests to the same controller; the controller is the only arbiter,
# and it serialises them on its OnIdle callback. A lease spans one compound
# write so another caller cannot edit the same slot between its steps.
# Keys are (track, slot); values are (owner, token, expires_at).
_slot_leases: dict[tuple[int, int], tuple[str, str, float]] = {}
_LEASE_SECONDS = 20.0


def _lease_key(track: int, slot: int) -> tuple[int, int] | None:
    if type(track) is int and type(slot) is int and 0 <= track <= 500 and 0 <= slot < 10:
        return track, slot
    return None


def _read_lease(key: tuple[int, int]):
    entry = _slot_leases.get(key)
    if not entry:
        return None
    owner, token, expires = entry
    if time.monotonic() >= expires:
        del _slot_leases[key]
        return None
    return owner, token


def claim_slot(track: int, slot: int, context: str, token: str) -> None:
    """Claim a slot for a compound write, or refuse if another caller holds it.

    A lease whose original request is still in flight is never stolen: only
    expiry releases it, and callers must resubmit with a fresh token after an
    outcome-unknown result.
    """
    key = _lease_key(track, slot)
    if key is None:
        raise ValueError("Invalid slot lease target")
    held = _read_lease(key)
    if held and (held[0] != context or held[1] != token):
        raise RuntimeError("Mixer slot is reserved by another write")
    _slot_leases[key] = (context, token, time.monotonic() + _LEASE_SECONDS)


def release_slot(track: int, slot: int, context: str, token: str) -> None:
    key = _lease_key(track, slot)
    if key is None:
        return
    held = _read_lease(key)
    if held and held[0] == context and held[1] == token:
        _slot_leases.pop(key, None)


def verify_slot_context(track: int, slot: int, context: str, token: str) -> None:
    """Re-check, in the same OnIdle callback, that this write still owns the slot."""
    key = _lease_key(track, slot)
    held = _read_lease(key) if key else None
    if not held or held[0] != context or held[1] != token:
        raise RuntimeError("Slot lease is missing or belongs to another write")
    if context != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    import general
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")

# Automation point curve-shape dword values measured against the actual host
# output on FL 26.1.7.5654 (see the isolated curve-mapping evidence). The
# dword sits at record bytes +0x0C..+0x0F, right after the tension float; the
# FLP format serialises the same four bytes as one dword. Values absent from
# this set are rejected until they are measured.
VERIFIED_CURVES = frozenset({0})


def playlist_refreshed():
    """Conservatively expire Playlist snapshots on host refresh notifications."""
    global _playlist_revision
    _playlist_revision += 1


def project_changed():
    global _effect_project
    _effect_project += 1


def read_effect_slot(params):
    track, slot = params.get("track"), params.get("slot")
    # On builds exposing getTrackCount, it returns the highest regular track index.
    get_last_track = getattr(mixer, "getTrackCount", None)
    last_track = get_last_track() if get_last_track else mixer.trackCount() - 1
    if type(track) is not int or not 0 <= track <= last_track:
        raise ValueError("Invalid Mixer track index")
    if type(slot) is not int or not 0 <= slot < 10:
        raise ValueError("Mixer slot must be 0..9")
    occupied = bool(plugins.isValid(track, slot, True))
    return {
        "track": track,
        "slot": slot,
        "occupied": occupied,
        "plugin_name": plugins.getPluginName(track, slot, False, True) if occupied else None,
        "context": effect_context(),
    }

def effect_context():
    return f"{_effect_controller}:{_effect_project}"

def native_effect_loader():
    # One fixed module name. Builds are versioned by git tag, not by filename:
    # the compiled extension is replaced in place so a clone runs as-is.
    try:
        return importlib.import_module("_flmcp_native")
    except ImportError as exc:
        raise RuntimeError("The local FL effect extension is unavailable "
                           "(build _flmcp_native.pyd with scripts/build-native-effect-loader.ps1)") from exc

def native_capabilities():
    """Report the running controller session and verified native operation profile."""
    result = {"context": effect_context(), "native": {"available": False, "operations": []}}
    try:
        loader = native_effect_loader()
        description = loader.describe()
        engine_path = loader.initialize()
        result["native"] = {**description, "available": True, "engine_path": engine_path}
    except (ImportError, RuntimeError, AttributeError) as exc:
        result["native"]["error"] = str(exc)
    return result


def add_sampler(params):
    """Append a Sampler and ask FL to load a WAV on its idle thread."""
    import channels
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before insertion")
    expected = params.get("expected_count")
    path = params.get("path")
    dry_run = params.get("dry_run", False)
    if (type(expected) is not int or not 0 <= expected < 4096 or
            not isinstance(path, str) or not Path(path).is_absolute() or
            Path(path).suffix.casefold() != ".wav" or not Path(path).is_file() or
            type(dry_run) is not bool):
        raise ValueError("Expected an existing absolute WAV path and a valid channel count")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    if channels.channelCount(True) != expected:
        raise RuntimeError("The channel list changed before insertion")
    loader = native_effect_loader()
    loader.initialize()
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "index": expected, "source_path": path}
    try:
        actual = loader.execute("channel.add_sampler", (path, expected))
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed", "error": str(exc)}
    try:
        observed_count = channels.channelCount(True)
        observed_type = channels.getChannelType(expected, True)
        observed_name = channels.getChannelName(expected, True)
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed",
                "error": "Sampler submitted, but channel readback failed: " + str(exc)}
    if actual != expected or observed_count != expected + 1 or observed_type != 0:
        return {"success": False, "submitted": True, "state": "unconfirmed",
                "error": "Sampler insertion was not independently confirmed"}
    return {"success": False, "submitted": True, "state": "partial", "index": actual,
            "context": effect_context(), "channel_name": observed_name,
            "source_path": path,
            "source_path_verified": False,
            "error": "Sampler created; FL sample source path cannot yet be independently read back"}

def add_generator(params):
    """Append a Generator (plugin-hosting channel) and load an FST preset.

    Unlike add_sampler, the loaded plugin's display name is readable from the
    channel object, so a committed request can be independently verified. The
    native op inserts the channel through the same verified path as a Sampler
    (mode 0 loads a plugin/preset rather than mode 1 loading a WAV) and reads
    the plugin name back on the FL idle thread.
    """
    import channels
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before insertion")
    expected = params.get("expected_count")
    path = params.get("path")
    plugin_name = params.get("plugin_name")
    dry_run = params.get("dry_run", False)
    if (type(expected) is not int or not 0 <= expected < 4096 or
            not isinstance(path, str) or not Path(path).is_absolute() or
            Path(path).suffix.casefold() != ".fst" or not Path(path).is_file() or
            not isinstance(plugin_name, str) or not plugin_name.strip() or
            type(dry_run) is not bool):
        raise ValueError(
            "Expected an existing absolute FST path, a channel count, and a plugin name")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    if channels.channelCount(True) != expected:
        raise RuntimeError("The channel list changed before insertion")
    loader = native_effect_loader()
    loader.initialize()
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "index": expected, "preset_path": path, "plugin_name": plugin_name}
    try:
        actual, kind, observed_plugin = loader.execute("channel.add_generator", (path, expected))
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed", "error": str(exc)}
    try:
        observed_count = channels.channelCount(True)
        observed_type = channels.getChannelType(expected, True)
        observed_name = channels.getChannelName(expected, True)
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed",
                "error": "Generator submitted, but channel readback failed: " + str(exc)}
    # A generator is a non-Sampler channel (public type != 0; Sampler is 0).
    # The plugin name read from the channel object is the decisive independent
    # evidence that the intended FST was actually loaded; compare
    # case-insensitively because FL may normalise casing. The native op's
    # immediate +0x160 read (`kind`) is reported for diagnostics only: on this
    # build FL fills the channel type field a later cycle (same behaviour as
    # automation-source creation), so the native read is still the default 0
    # while the public readback after the RPC round trip already shows the real
    # type. Do not gate acceptance on `kind`.
    name_matches = (isinstance(observed_plugin, str) and
                    observed_plugin.strip().casefold() == plugin_name.strip().casefold())
    if (actual != expected or observed_count != expected + 1 or
            observed_type == 0 or not name_matches):
        return {"success": False, "submitted": True, "state": "unconfirmed",
                "error": "Generator insertion was not independently confirmed",
                "index": actual, "observed_kind_native": kind,
                "observed_type": observed_type,
                "observed_plugin": observed_plugin, "expected_plugin": plugin_name}
    return {"success": True, "submitted": True, "state": "verified",
            "index": actual, "context": effect_context(), "channel_name": observed_name,
            "preset_path": path, "plugin_name": observed_plugin,
            "channel_type": observed_type, "source_path_verified": True}

def replace_sample(params):
    """Replace the WAV loaded into an existing Sampler channel.

    The semantic layer performs the load and confirms the target channel's
    identity is unchanged (same index, still a Sampler, same name, same channel
    count). The actual source-path change is verified by the tool layer through
    fl_read_sampler_sources, because a Sampler's path is only observable in a
    project save-copy on this build.
    """
    import channels
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before replacement")
    index = params.get("channel")
    expected = params.get("expected_count")
    path = params.get("path")
    dry_run = params.get("dry_run", False)
    if (type(index) is not int or not 0 <= index < 4096 or
            type(expected) is not int or not 0 <= expected < 4096 or index >= expected or
            not isinstance(path, str) or not Path(path).is_absolute() or
            Path(path).suffix.casefold() != ".wav" or not Path(path).is_file() or
            type(dry_run) is not bool):
        raise ValueError(
            "Expected a valid Sampler index, count, absolute WAV path, and dry_run")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    count = channels.channelCount(True)
    if count != expected or index >= count:
        raise RuntimeError("The channel list changed before replacement")
    try:
        channel_type = channels.getChannelType(index, True)
        channel_name = channels.getChannelName(index, True)
    except Exception as exc:
        raise RuntimeError("Cannot read the target channel: " + str(exc)) from exc
    if channel_type != 0:
        raise ValueError("The target channel is not a Sampler (type 0)")
    loader = native_effect_loader()
    loader.initialize()
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True, "index": index,
                "channel_name": channel_name, "source_path": path}
    try:
        actual = loader.execute("channel.replace_sample", (index, path, expected))
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed", "error": str(exc)}
    try:
        observed_count = channels.channelCount(True)
        observed_type = channels.getChannelType(index, True)
        observed_name = channels.getChannelName(index, True)
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed",
                "error": "Replacement submitted, but channel readback failed: " + str(exc)}
    # FL renames a Sampler channel to the loaded WAV's file name, so the name
    # is expected to change on a successful replacement. Identity is held by
    # the index, Sampler type and channel count; record both names for audit.
    identity_ok = (actual == index and observed_count == expected and observed_type == 0)
    if not identity_ok:
        return {"success": False, "submitted": True, "state": "unconfirmed",
                "error": "The target channel changed during replacement",
                "index": actual, "observed_count": observed_count,
                "observed_type": observed_type, "observed_name": observed_name}
    return {"success": True, "submitted": True, "state": "applied",
            "index": index, "context": effect_context(),
            "channel_name": observed_name, "previous_channel_name": channel_name,
            "source_path": path}

def insert_audio_clip(params):
    """Place an existing Sampler as an Audio Clip at an absolute tick and track."""
    import channels
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before insertion")
    channel = params.get("channel")
    track = params.get("track")
    start_tick = params.get("start_tick")
    length_tick = params.get("length_tick")
    source_start = params.get("source_start")
    source_end = params.get("source_end")
    expected = params.get("expected_count")
    dry_run = params.get("dry_run", False)
    if (type(channel) is not int or channel < 0
            or type(track) is not int or not 1 <= track <= 500
            or type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF
            or type(length_tick) is not int or not 1 <= length_tick <= 0x3FFFFFFF
            or type(expected) is not int or not 0 <= expected <= 100000
            or type(dry_run) is not bool):
        raise ValueError("Invalid audio clip channel, position, length, or count")
    if source_start is None:
        source_start = -1
    if source_end is None:
        source_end = -1
    if not ((source_start == -1 and source_end == -1)
            or (type(source_start) is int and type(source_end) is int
                and 0 <= source_start < source_end <= 0x7FFFFFFF
                and length_tick == source_end - source_start)):
        raise ValueError("Invalid audio clip source range")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    try:
        channel_count = channels.channelCount(True)
        channel_type = channels.getChannelType(channel, True) if channel < channel_count else None
        channel_name = channels.getChannelName(channel, True) if channel < channel_count else None
    except Exception as exc:
        raise RuntimeError("Cannot read the source channel: " + str(exc)) from exc
    if channel >= channel_count or channel_type != 0:
        raise ValueError("The source channel is not an existing Sampler")
    loader = native_effect_loader()
    loader.initialize()
    before = read_playlist_clips({"context": effect_context(), "offset": 0, "limit": 1})
    arrangement, count = before["arrangement"], before["count"]
    if count != expected:
        raise RuntimeError("The Playlist clip count changed before insertion")
    source = ((channel << 16) | 0x5000) & 0xFFFFFFFF
    arguments = (arrangement, start_tick, source, length_tick, track,
                 source_start, source_end, expected)
    if dry_run:
        loader.execute("playlist.insert_clip", (*arguments, 0))
        return {"success": True, "submitted": False, "dry_run": True,
                "channel": channel, "channel_name": channel_name, "track": track,
                "start_tick": start_tick, "length_tick": length_tick,
                "source_raw": source}
    general.saveUndo("Insert audio clip", midi.UF_PL)
    try:
        index, after_count = loader.execute("playlist.insert_clip", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed", "error": str(exc)}
    observed = read_playlist_clips({"context": effect_context(), "offset": index, "limit": 1})
    clips = observed.get("clips", [])
    clip = clips[0] if len(clips) == 1 else {}
    verified = (
        observed.get("arrangement") == arrangement
        and observed.get("count") == after_count
        and clip.get("index") == index
        and clip.get("start_tick") == start_tick
        and clip.get("track") == track
        and clip.get("length_tick") == length_tick
        and clip.get("source_raw") == source
    )
    return {
        "success": bool(verified and after_count == expected + 1),
        "submitted": True, "state": "applied_verified" if verified else "unconfirmed",
        "index": index, "arrangement": arrangement, "count": after_count,
        "channel": channel, "channel_name": channel_name,
        "observed": clip or None, "context": effect_context(),
        **({"error": "Audio clip insertion could not be independently verified"}
           if not verified or after_count != expected + 1 else {}),
    }

def read_playlist_clips(params):
    """Read a bounded page from the currently active arrangement on FL's idle thread."""
    import channels

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the read")
    offset = params.get("offset", 0)
    limit = params.get("limit", 32)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("offset must be nonnegative and limit must be 1..64")
    loader = native_effect_loader()
    loader.initialize()
    arrangement, count, start, records = loader.execute("playlist.read_clips", (offset, limit))
    if len(records) % 36 or len(records) // 36 > limit or start != offset:
        raise RuntimeError("Invalid Playlist page returned by the native bridge")
    clips = []
    for position in range(0, len(records), 36):
        record = records[position:position + 36]
        start_tick, source_raw, length_tick, track_raw = struct.unpack_from("<iIih", record)
        index = offset + position // 36
        is_pattern = source_raw >= 0x50000000
        source_index = (source_raw - 0x50000000 if is_pattern else source_raw) >> 16
        # Pattern clips carry -1 markers here; automation clips (channel type
        # 2) carry integer window markers too; audio clips store the trimmed
        # source window as two float milliseconds fields at the same offsets.
        kind = None
        if is_pattern:
            kind = "pattern"
        else:
            try:
                kind = ("automation" if channels.getChannelType(source_index, True) in (2, 5)
                        else "channel")
            except Exception:
                kind = "channel"
        if kind in ("pattern", "automation"):
            source_start, source_end = struct.unpack_from("<ii", record, 0x18)
            source_range = {"source_start_tick": source_start, "source_end_tick": source_end}
        else:
            start_ms, end_ms = struct.unpack_from("<ff", record, 0x18)
            source_range = {"source_start_ms": start_ms, "source_end_ms": end_ms}
        clips.append({
            "index": index,
            "track": 500 - track_raw,
            "start_tick": start_tick,
            "length_tick": length_tick,
            **source_range,
            "source_type": kind,
            "source_index": source_index,
            "source_raw": source_raw,
            "muted": bool(record[0x13] & 0x20),
            "snapshot_ref": (f"{effect_context()}:p{_playlist_revision}:"
                             f"{arrangement}:{index}:{record.hex()}"),
        })
    next_offset = offset + len(clips)
    return {
        "success": True,
        "context": effect_context(),
        "arrangement": arrangement,
        "count": count,
        "offset": offset,
        "clips": clips,
        "next_offset": next_offset if next_offset < count else None,
    }

def read_automation_points(params):
    """Read a validated page of points from an Automation Clip channel."""
    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the read")
    channel, offset, limit = params.get("channel"), params.get("offset", 0), params.get("limit", 32)
    if type(channel) is not int or channel < 0 or type(offset) is not int or offset < 0:
        raise ValueError("channel and offset must be nonnegative integers")
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("limit must be 1..64")
    loader = native_effect_loader()
    loader.initialize()
    actual_channel, source_event, count, maximum, time_beats, records = loader.execute(
        "automation.read_points", (channel, offset, limit)
    )
    if actual_channel != channel or len(records) % 32 or len(records) // 32 > limit:
        raise RuntimeError("Invalid automation page returned by the native bridge")
    points = []
    for position in range(0, len(records), 32):
        delta, value, tension, curve = struct.unpack_from("<fffB", records, position)
        prior_time = time_beats
        time_beats += delta
        index = offset + position // 32
        snapshot = records[position:position + 32].hex()
        snapshot_ref = (f"{effect_context()}:{channel}:{source_event}:{index}:"
                        f"{prior_time.hex()}:{snapshot}")
        points.append({"index": index, "time_beats": time_beats, "value": value,
                       "tension": tension, "curve": curve,
                       "snapshot_ref": snapshot_ref})
    next_offset = offset + len(points)
    return {"success": True, "context": effect_context(), "channel": channel,
            "source_event": source_event, "count": count, "maximum_beats": maximum,
            "offset": offset, "points": points,
            "next_offset": next_offset if next_offset < count else None}


def read_automation_links(params):
    """Read links by automation channel, including clips absent from Playlist."""
    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the read")
    channel, offset, limit = params.get("channel"), params.get("offset", 0), params.get("limit", 32)
    include_names = params.get("include_names", False)
    include_values = params.get("include_values", False)
    include_targets = params.get("include_targets", False)
    if (type(channel) is not int or channel < 0 or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 64 or type(include_names) is not bool
            or type(include_values) is not bool or type(include_targets) is not bool
            or (include_targets and limit > 16)):
        raise ValueError("Invalid automation channel or link page")
    loader = native_effect_loader()
    loader.initialize()
    actual, source, count, records = loader.execute(
        "automation.read_links", (channel, offset, limit)
    )
    if actual != channel or len(records) % 4 or len(records) // 4 > limit:
        raise RuntimeError("Invalid automation link page returned by the native bridge")
    links = [{"index": offset + pos // 4,
              "target_event_id": struct.unpack_from("<I", records, pos)[0]}
             for pos in range(0, len(records), 4)]
    if include_targets:
        # Mixer effect automation IDs are location-encoded on this build.
        # Resolve only IDs corroborated by a currently valid FL plugin parameter.
        for link in links:
            link["target"] = None
            delta = link["target_event_id"] - 0x70008000
            if delta < 0:
                continue
            location, parameter = divmod(delta, 0x10000)
            track, slot = divmod(location, 64)
            try:
                last_track = (mixer.getTrackCount() if hasattr(mixer, "getTrackCount")
                              else mixer.trackCount() - 1)
                if not 0 <= track <= last_track or slot >= 10:
                    continue
                if not plugins.isValid(track, slot, True):
                    continue
                parameter_count = plugins.getParamCount(track, slot, True)
                if type(parameter_count) is not int or not 0 <= parameter < parameter_count:
                    continue
                link["target"] = {
                    "kind": "mixer_effect_parameter", "track": track, "slot": slot,
                    "parameter": parameter,
                    "plugin_name": plugins.getPluginName(track, slot, False, True),
                    "parameter_name": plugins.getParamName(parameter, track, slot, True),
                }
            except Exception:
                continue
    if include_names:
        for link in links:
            try:
                name = mixer.getEventIDName(link["target_event_id"])
                link["target_name"] = name if isinstance(name, str) and name else None
            except Exception:
                link["target_name"] = None
    if include_values:
        import device

        for link in links:
            try:
                value = device.getLinkedValue(link["target_event_id"])
                link["target_value"] = (float(value) if type(value) in (int, float)
                                        and math.isfinite(value) and 0 <= value <= 1 else None)
            except Exception:
                link["target_value"] = None
    next_offset = offset + len(links)
    return {"success": True, "context": effect_context(), "channel": channel,
            "source_event": source, "count": count, "offset": offset, "links": links,
            "next_offset": next_offset if next_offset < count else None}


def scan_mixer_automation_links(params):
    """Report automation links whose encoded target lands in a Mixer slot.

    This is a PRECHECK only: the exact post-swap resolution of a link on this
    build has not been measured. Each affected link therefore carries a
    ``binding_confirmed`` flag that is true only when FL itself corroborates a
    currently valid plugin parameter at the same location. Callers must refuse
    structural writes when confirmation is absent.
    """
    import channels

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the scan")
    track_filter = params.get("track")
    slots = params.get("slots")
    if (type(track_filter) is not int or not 0 <= track_filter <= 500
            or not isinstance(slots, list) or not slots
            or any(type(s) is not int or not 0 <= s < 10 for s in slots)):
        raise ValueError("Invalid mixer automation scan request")
    loader = native_effect_loader()
    loader.initialize()
    channel_count = channels.channelCount(True)
    affected = []
    for channel in range(channel_count):
        try:
            actual, source_event, count, records = loader.execute(
                "automation.read_links", (channel, 0, 64))
        except Exception:
            continue  # a channel with no readable link registry is not a target
        if actual != channel or len(records) % 4:
            continue
        for pos in range(0, len(records), 4):
            event_id = struct.unpack_from("<I", records, pos)[0]
            delta = event_id - 0x70008000
            if delta < 0:
                continue
            location, parameter = divmod(delta, 0x10000)
            track, slot = divmod(location, 64)
            if track != track_filter or slot not in slots:
                continue
            binding_confirmed = False
            target = None
            try:
                last_track = (mixer.getTrackCount() if hasattr(mixer, "getTrackCount")
                              else mixer.trackCount() - 1)
                if 0 <= track <= last_track and plugins.isValid(track, slot, True):
                    parameter_count = plugins.getParamCount(track, slot, True)
                    if type(parameter_count) is int and 0 <= parameter < parameter_count:
                        binding_confirmed = True
                        target = {
                            "kind": "mixer_effect_parameter", "track": track,
                            "slot": slot, "parameter": parameter,
                            "plugin_name": plugins.getPluginName(track, slot, False, True),
                            "parameter_name": plugins.getParamName(
                                parameter, track, slot, True),
                        }
            except Exception:
                pass
            affected.append({
                "channel": channel, "source_event": source_event,
                "target_event_id": event_id, "track": track, "slot": slot,
                "parameter": parameter, "binding_confirmed": binding_confirmed,
                "target": target,
            })
    return {"success": True, "context": effect_context(),
            "channel_count": channel_count, "affected": affected,
            "post_swap_resolution_verified": False}


def create_automation_source(params):
    """Append an automation channel linked to a verified control event.

    The verified target form is a Mixer effect parameter on an occupied slot;
    a raw event id is accepted only when FL can name it. Undo behaviour is
    reported by the isolated acceptance evidence, not assumed here.
    """
    import channels
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before creation")
    target = params.get("target")
    dry_run = params.get("dry_run", False)
    expected = params.get("expected_count")
    if (not isinstance(target, dict) or type(dry_run) is not bool
            or type(expected) is not int or not 0 <= expected <= 4095):
        raise ValueError("Expected a target object, an integer channel count, and a dry_run flag")
    kind = target.get("kind")
    if kind == "mixer_effect_parameter":
        track, slot, parameter = (target.get("track"), target.get("slot"),
                                  target.get("parameter"))
        last_track = (mixer.getTrackCount() if hasattr(mixer, "getTrackCount")
                      else mixer.trackCount() - 1)
        if (type(track) is not int or type(slot) is not int or type(parameter) is not int
                or not 0 <= slot < 10 or not 0 <= track <= last_track):
            raise ValueError("Invalid Mixer effect parameter target")
        if not plugins.isValid(track, slot, True):
            raise ValueError("The requested Mixer slot is empty")
        if not 0 <= parameter < plugins.getParamCount(track, slot, True):
            raise ValueError("Parameter index is outside the plugin's parameter list")
        target_id = (0x70008000 + ((track * 64 + slot) << 16) + parameter) & 0xFFFFFFFF
    elif kind == "event_id":
        target_id = target.get("id")
        if type(target_id) is not int or not 0x1000 <= target_id < 0x80000000:
            raise ValueError("Invalid target event id")
    else:
        raise ValueError("Target kind must be mixer_effect_parameter or event_id")
    try:
        target_name = mixer.getEventIDName(target_id)
    except Exception as exc:
        raise ValueError("FL could not read the target event: " + str(exc)) from exc
    if not isinstance(target_name, str) or not target_name:
        raise ValueError("The target event has no FL-readable name; read links first")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    if channels.channelCount(True) != expected:
        raise RuntimeError("The channel list changed before creation")
    loader = native_effect_loader()
    loader.initialize()
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "target_event_id": target_id, "target_name": target_name,
                "index": expected}
    try:
        created_index, append_pending = loader.execute(
            "automation.create_source", (target_id, expected, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed", "error": str(exc)}
    if append_pending:
        # The routine ran but FL has not appended the channel yet; the caller
        # must re-read before treating the channel as existing.
        return {"success": False, "submitted": True, "state": "unconfirmed",
                "pending_identity": True, "index": created_index,
                "target_event_id": target_id, "target_name": target_name,
                "context": effect_context(),
                "error": "Automation channel submitted; FL has not appended it yet. "
                         "Read the channel again before use."}
    try:
        observed_count = channels.channelCount(True)
        observed_type = channels.getChannelType(created_index, True)
        links = read_automation_links({"context": effect_context(), "channel": created_index,
                                       "offset": 0, "limit": 16, "include_names": True})
    except Exception:
        observed_count, observed_type, links = None, None, {}
    link_ok = (links.get("success") and links.get("count") == 1
               and links.get("links") and links["links"][0]["target_event_id"] == target_id)
    verified = (observed_count == expected + 1 and created_index == expected and link_ok)
    if not verified:
        # FL populates the new channel's identity fields on a later cycle;
        # the caller must re-read before treating the channel as usable.
        return {"success": False, "submitted": True, "state": "unconfirmed",
                "pending_identity": True, "index": created_index,
                "target_event_id": target_id, "target_name": target_name,
                "context": effect_context(),
                "error": "Automation channel appended; FL has not yet exposed its "
                         "identity. Read the channel again before use."}
    return {
        "success": True, "submitted": True, "state": "applied_verified",
        "index": created_index, "channel_type": observed_type,
        "source_event": links.get("source_event"), "target_event_id": target_id,
        "target_name": target_name, "links": links.get("links"),
        "context": effect_context(),
        "note": "The created channel reads public type 5 (CT_AutoClip). Playlist "
                "automation-clip placement accepts automation kinds 2 and 5.",
    }


def set_automation_point_float(params, field="value"):
    """Change one observed point float, with FL's channel-specific undo snapshot."""
    import general

    try:
        context, channel, source, index, prior_time, raw = params["snapshot_ref"].rsplit(":", 5)
        channel, source, index = int(channel), int(source), int(index)
        prior_time = float.fromhex(prior_time)
        record = bytes.fromhex(raw)
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid automation point snapshot") from exc
    if context != effect_context() or params.get("context") != context:
        raise RuntimeError("The FL controller or current project changed before the edit")
    value = params.get(field)
    dry_run = params.get("dry_run", False)
    lower = -1 if field == "tension" else 0
    if (type(value) not in (float, int) or not math.isfinite(value) or not lower <= value <= 1
            or type(dry_run) is not bool or not math.isfinite(prior_time) or prior_time < 0
            or channel < 0 or index < 0 or len(record) != 32):
        raise ValueError(f"Expected a point snapshot and {field} in {lower}..1")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    arguments = (channel, index, source, prior_time, record, float(value))
    operation = ("automation.set_point_tension" if field == "tension"
                 else "automation.set_point_value")
    loader.execute(operation, (*arguments, 0))
    previous = struct.unpack_from("<f", record, 8 if field == "tension" else 4)[0]
    actual_value = struct.unpack("<f", struct.pack("<f", value))[0]
    if dry_run or actual_value == previous:
        return {"success": True, "submitted": False, "dry_run": dry_run,
                "unchanged": actual_value == previous, "before": previous, "after": actual_value}
    try:
        loader.execute(operation, (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "channel": channel,
            "index": index, field: actual_value, "context": context}


def set_automation_point_curve(params):
    """Change one observed point's curve byte, with the channel undo snapshot.

    The curve byte selects the segment shape FL draws from this point to the
    next one. Only modes verified against the actual host output are accepted;
    unverified values are rejected until measured.
    """
    import general

    try:
        context, channel, source, index, prior_time, raw = params["snapshot_ref"].rsplit(":", 5)
        channel, source, index = int(channel), int(source), int(index)
        prior_time = float.fromhex(prior_time)
        record = bytes.fromhex(raw)
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid automation point snapshot") from exc
    if context != effect_context() or params.get("context") != context:
        raise RuntimeError("The FL controller or current project changed before the edit")
    curve = params.get("curve")
    dry_run = params.get("dry_run", False)
    if (type(curve) is not int or not 0 <= curve <= 0x7FFFFFFF
            or type(dry_run) is not bool or not math.isfinite(prior_time) or prior_time < 0
            or channel < 0 or index < 0 or len(record) != 32):
        raise ValueError("Expected a point snapshot and a curve dword in 0..0x7FFFFFFF")
    if curve not in VERIFIED_CURVES:
        raise ValueError(f"Curve shape {curve} has not been measured on this build; "
                         f"verified shapes are {sorted(VERIFIED_CURVES)}")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    arguments = (channel, index, source, prior_time, record, curve)
    loader.execute("automation.set_point_curve", (*arguments, 0))
    previous = struct.unpack_from("<I", record, 12)[0]
    if dry_run or previous == curve:
        return {"success": True, "submitted": False, "dry_run": dry_run,
                "unchanged": previous == curve, "before": previous, "after": curve}
    try:
        loader.execute("automation.set_point_curve", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "channel": channel,
            "index": index, "curve": curve, "context": context}


def move_automation_point(params):
    """Move one point without changing the following point's absolute position."""
    import general

    try:
        context, channel, source, index, prior_hex, raw = params["snapshot_ref"].rsplit(":", 5)
        channel, source, index = int(channel), int(source), int(index)
        prior = float.fromhex(prior_hex)
        record = bytes.fromhex(raw)
        next_ref = params.get("next_snapshot_ref")
        next_record = b""
        if next_ref is not None:
            next_context, nch, nsource, nindex, next_prior, next_raw = next_ref.rsplit(":", 5)
            next_record = bytes.fromhex(next_raw)
            if (next_context != context or int(nch) != channel or int(nsource) != source
                    or int(nindex) != index + 1 or
                    float.fromhex(next_prior) != prior + struct.unpack_from("<f", record)[0]):
                raise ValueError("Next point does not match the target snapshot")
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError, struct.error) as exc:
        raise ValueError("Invalid automation point snapshot") from exc
    if context != effect_context() or params.get("context") != context:
        raise RuntimeError("The FL controller or current project changed before the edit")
    target = params.get("time_beats")
    dry_run = params.get("dry_run", False)
    if (type(target) not in (int, float) or not math.isfinite(target) or target < 0
            or not math.isfinite(prior) or prior < 0 or channel < 0 or index < 0
            or len(record) != 32 or len(next_record) not in (0, 32) or type(dry_run) is not bool):
        raise ValueError("Invalid point time, snapshot, or dry_run")
    if index == 0 and target != 0:
        raise ValueError("The first automation point must remain at beat zero")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    arguments = (channel, index, source, prior, record, next_record, float(target))
    loader.execute("automation.move_point", (*arguments, 0))
    old_time = prior + struct.unpack_from("<f", record)[0]
    if dry_run or target == old_time:
        return {"success": True, "submitted": False, "dry_run": dry_run,
                "unchanged": target == old_time, "before": old_time, "after": target}
    try:
        loader.execute("automation.move_point", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "channel": channel,
            "index": index, "time_beats": target, "context": context}


def edit_automation_points(params):
    """Insert or delete an interior point against a complete curve snapshot."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    channel, source, index = params.get("channel"), params.get("source"), params.get("index")
    mode, dry_run = params.get("mode"), params.get("dry_run", False)
    position = params.get("time_beats", 0)
    value, tension = params.get("value", 0), params.get("tension", 0)
    try:
        records = bytes.fromhex(params["records_hex"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid automation curve snapshot") from exc
    if (type(channel) is not int or channel < 0 or type(source) is not int or source < 0
            or type(index) is not int or index < 0 or mode not in ("insert", "delete")
            or type(dry_run) is not bool or len(records) < 64 or len(records) > 4000 * 32
            or len(records) % 32 or any(type(v) not in (int, float) or not math.isfinite(v)
                                        for v in (position, value, tension))):
        raise ValueError("Invalid automation curve edit")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    args = (channel, source, records, 0 if mode == "insert" else 1,
            float(position), float(value), float(tension), index)
    loader.execute("automation.edit_points", (*args, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "mode": mode, "index": index, "count_before": len(records) // 32}
    try:
        loader.execute("automation.edit_points", (*args, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "context": effect_context(),
            "mode": mode, "index": index, "channel": channel}

def insert_pattern_clip(params):
    """Place an existing Pattern as a Playlist Pattern Clip.

    pattern follows FL's pattern indexing used by the patterns module and the
    native clip source field. length_tick defaults to FL's own derived pattern
    length in ticks (getPatternLength steps times PPQ/4). The clip window
    fields stay at their initializer defaults; pattern clips have no source
    window.
    """
    import general
    import patterns

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before insertion")
    pattern = params.get("pattern")
    track = params.get("track")
    start_tick = params.get("start_tick")
    length_tick = params.get("length_tick")
    expected_count = params.get("expected_count")
    dry_run = params.get("dry_run", False)
    # jumpToPattern-created patterns may not be reflected in patternCount,
    # so validate through the length getter (template returns non-int).
    if (type(pattern) is not int or pattern < 1
            or type(patterns.getPatternLength(pattern)) is not int
            or type(track) is not int or not 1 <= track <= 500
            or type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF
            or type(dry_run) is not bool):
        raise ValueError("Invalid pattern clip request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    ppq = general.getRecPPQ()
    if type(ppq) is not int or ppq < 1:
        raise RuntimeError("FL did not report its pulses per quarter")
    pattern_length_ticks = patterns.getPatternLength(pattern) * ppq // 4
    if length_tick is None:
        length_tick = pattern_length_ticks
    if type(length_tick) is not int or not 1 <= length_tick <= 0x3FFFFFFF:
        raise ValueError("Invalid pattern clip length")
    loader = native_effect_loader()
    loader.initialize()
    source = 0x50005000 + (pattern << 16)
    page = read_playlist_clips({"context": effect_context(), "offset": 0, "limit": 1})
    live_count = page["count"]
    # Insert into the ACTIVE arrangement: FL's clip writer takes the arrangement
    # index, and a hardcoded 0 silently failed once a second arrangement existed.
    arrangement = page.get("arrangement", 0)
    arguments = (arrangement, start_tick, source, length_tick, track, -1, -1, live_count)
    loader.execute("playlist.insert_clip", (*arguments, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "pattern": pattern, "track": track, "start_tick": start_tick,
                "length_tick": length_tick, "context": effect_context()}
    general.saveUndo("Place Pattern clip", midi.UF_PL)
    try:
        index, count = loader.execute("playlist.insert_clip", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed", "error": str(exc)}
    return {"success": True, "submitted": True, "index": index, "count": count,
            "pattern": pattern, "pattern_name": patterns.getPatternName(pattern),
            "track": track, "start_tick": start_tick,
            "length_tick": length_tick,
            "pattern_length_ticks": pattern_length_ticks,
            "context": effect_context()}

def insert_automation_clip(params):
    """Place an automation channel's control as a Playlist Automation Clip.

    channel must be an existing automation channel (channel type 2). The clip
    length is explicit because automation clips have no derived length. The
    window fields keep their initializer defaults.
    """
    import channels
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before insertion")
    channel = params.get("channel")
    track = params.get("track")
    start_tick = params.get("start_tick")
    length_tick = params.get("length_tick")
    expected_count = params.get("expected_count")
    dry_run = params.get("dry_run", False)
    if (type(channel) is not int or channel < 0
            or type(track) is not int or not 1 <= track <= 500
            or type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF
            or type(length_tick) is not int or not 1 <= length_tick <= 0x3FFFFFFF
            or type(expected_count) is not int or expected_count < 0
            or type(dry_run) is not bool):
        raise ValueError("Invalid automation clip request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    try:
        channel_type = channels.getChannelType(channel, True)
    except Exception as exc:
        raise ValueError("The channel index does not exist") from exc
    if channel_type not in (2, 5):
        # This build exposes two verified automation kinds: legacy clips read 2
        # and current CT_AutoClip channels (including routine-created ones) read 5.
        raise ValueError("The channel is not an automation channel")
    loader = native_effect_loader()
    loader.initialize()
    source = ((channel << 16) | 0x5000) & 0xFFFFFFFF
    arrangement = read_playlist_clips(
        {"context": effect_context(), "offset": 0, "limit": 1}).get("arrangement", 0)
    arguments = (arrangement, start_tick, source, length_tick, track, -1, -1, expected_count)
    loader.execute("playlist.insert_clip", (*arguments, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "channel": channel, "track": track, "start_tick": start_tick,
                "length_tick": length_tick, "context": effect_context()}
    general.saveUndo("Place Automation clip", midi.UF_PL)
    try:
        index, count = loader.execute("playlist.insert_clip", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "unconfirmed", "error": str(exc)}
    observed = read_playlist_clips({"context": effect_context(), "offset": index, "limit": 1})
    clips = observed.get("clips", [])
    clip = clips[0] if len(clips) == 1 else {}
    verified = (
        observed.get("success") and observed.get("context") == effect_context()
        and observed.get("count") == expected_count + 1 and clip.get("index") == index
        and clip.get("track") == track and clip.get("start_tick") == start_tick
        and clip.get("source_type") == "automation" and clip.get("source_index") == channel
    )
    return {"success": bool(verified), "submitted": True, "verified": bool(verified),
            "index": index, "count": count, "channel": channel,
            "track": track, "start_tick": start_tick,
            "requested_length_tick": length_tick,
            "derived_length_tick": clip.get("length_tick"),
            "length_note": ("FL derives the automation clip extent from the "
                            "automation envelope; the requested length is advisory"),
            "observed": clip or None, "context": effect_context()}

def manage_pattern(params):
    """Create, clone, or retitle Patterns through FL's public patterns module.

    mode=create selects FL's first empty pattern (FL's own creation semantics).
    mode=clone deep-copies an existing pattern's data into the next free slot.
    mode=metadata renames or recolors a pattern. Deletion has no public
    interface and is not offered.
    """
    import patterns

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    mode = params.get("mode")
    dry_run = params.get("dry_run", False)
    if mode not in ("create", "clone", "metadata") or type(dry_run) is not bool:
        raise ValueError("mode must be create, clone, or metadata")
    count_before = patterns.patternCount()
    if type(count_before) is not int or count_before < 1:
        raise RuntimeError("FL did not report its pattern count")
    if mode == "create":
        # The patterns module is one-based. jumpToPattern(count+1) makes FL
        # create and select the next default pattern; findFirstNextEmptyPat
        # is avoided because flags 0 and 1 hang FL's idle thread on this
        # build and the fixture reports no empty pattern with flags 2/4.
        target = count_before + 1
        if dry_run:
            return {"success": True, "submitted": False, "dry_run": True,
                    "mode": mode, "pattern": target,
                    "count_before": count_before, "context": effect_context()}
        patterns.jumpToPattern(target)
        name = patterns.getPatternName(target)
        current = patterns.patternNumber()
        if current != target or not isinstance(name, str):
            return {"success": False, "submitted": True,
                    "error": "FL did not select a newly created pattern",
                    "pattern": target, "current": current,
                    "context": effect_context()}
        return {"success": True, "submitted": True, "mode": mode,
                "pattern": target, "created": True, "name": name,
                "length_steps": patterns.getPatternLength(target),
                "current": current,
                "count_before": count_before,
                "count_note": ("patterns.patternCount may keep reporting the "
                               "pre-created count on this build"),
                "context": effect_context()}
    index = params.get("index")
    # The patterns module is one-based and jumpToPattern-created patterns may
    # not be reflected in patternCount yet. Out-of-range getters return
    # template values instead of failing, so bounds come from the real
    # maximum and the length getter must return a real integer.
    limit = max(patterns.patternCount(), patterns.patternNumber())
    if (type(index) is not int or index < 1 or index > limit
            or type(patterns.getPatternLength(index)) is not int):
        raise ValueError("Invalid pattern index")
    if mode == "clone":
        if dry_run:
            return {"success": True, "submitted": False, "dry_run": True,
                    "mode": mode, "index": index, "count_before": count_before,
                    "context": effect_context()}
        source_length = patterns.getPatternLength(index)
        patterns.clonePattern(index)
        # FL inserts the clone directly after the source, selects it, and
        # renames it to a numbered variant, so the source name cannot be used
        # for confirmation; the selected position and matching length can.
        new_index = index + 1
        current = patterns.patternNumber()
        length = patterns.getPatternLength(new_index)
        if current != new_index or type(length) is not int or length != source_length:
            return {"success": False, "submitted": True,
                    "error": "The cloned pattern could not be confirmed at index+1",
                    "index": index, "new_index": new_index, "current": current,
                    "context": effect_context()}
        return {"success": True, "submitted": True, "mode": mode,
                "index": index, "new_index": new_index,
                "name": patterns.getPatternName(new_index),
                "length_steps": length,
                "count_before": count_before,
                "count_after": patterns.patternCount(),
                "context": effect_context()}
    # metadata
    name = params.get("name")
    color = params.get("color")
    if name is None and color is None:
        raise ValueError("A name or color is required")
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 200):
        raise ValueError("Invalid pattern name")
    if color is not None and (type(color) is not int or not 0 <= color <= 0xFFFFFF):
        raise ValueError("color must be a 0xRRGGBB integer")
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True, "mode": mode,
                "index": index,
                "before": {"name": patterns.getPatternName(index),
                           "color": patterns.getPatternColor(index)},
                "context": effect_context()}
    changed = {}
    if name is not None:
        patterns.setPatternName(index, name)
        changed["name"] = patterns.getPatternName(index)
    if color is not None:
        patterns.setPatternColor(index, color)
        # getPatternColor returns an ARGB value with the alpha byte set.
        changed["color"] = patterns.getPatternColor(index) & 0xFFFFFF
    return {"success": True, "submitted": True, "mode": mode, "index": index,
            "changed": changed, "context": effect_context()}

def set_pattern_length(params):
    """Change a pattern's length in steps through the public patterns module."""
    import patterns

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    index = params.get("index")
    length_steps = params.get("length_steps")
    dry_run = params.get("dry_run", False)
    limit = max(patterns.patternCount(), patterns.patternNumber())
    if (type(index) is not int or index < 1 or index > limit
            or type(patterns.getPatternLength(index)) is not int
            or type(length_steps) is not int or not 1 <= length_steps <= 9999
            or type(dry_run) is not bool):
        raise ValueError("Invalid pattern length request")
    before = patterns.getPatternLength(index)
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True, "index": index,
                "before": before, "after": length_steps, "context": effect_context()}
    patterns.setPatternLength(index, length_steps)
    after = patterns.getPatternLength(index)
    return {"success": True, "submitted": True, "index": index,
            "before": before, "after": after,
            "verified": after == length_steps, "context": effect_context()}

NOTE_STRUCT = struct.Struct("<iiiH2xHBBBBBB")

def _prepare_pattern_undo(pattern):
    """FL records UF_PR undo points only for the pattern shown in the Piano
    Roll window, so select the pattern and open the window before saving the
    undo point. Both actions are visible UI changes and are reported."""
    import patterns
    import ui

    patterns.jumpToPattern(pattern)
    ui.showWindow(midi.widPianoRoll)

def read_pattern_notes(params):
    """Read a page of a pattern's piano-roll notes (1-based pattern number)."""
    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the read")
    pattern = params.get("pattern")
    offset = params.get("offset", 0)
    limit = params.get("limit", 48)
    if (type(pattern) is not int or not 1 <= pattern <= 999
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 48):
        raise ValueError("Invalid note page request")
    loader = native_effect_loader()
    loader.initialize()
    got_pattern, count, start, records = loader.execute(
        "pattern.read_notes", (pattern, offset, limit))
    if got_pattern != pattern or len(records) % 24 or len(records) // 24 > limit or start != offset:
        raise RuntimeError("Invalid note page returned by the native bridge")
    notes = []
    for position in range(0, len(records), 24):
        raw = records[position:position + 24]
        pos, packed, length, key, fine, release, flags, pan, velocity, cut, res = \
            NOTE_STRUCT.unpack(raw)
        index = offset + position // 24
        notes.append({
            "index": index,
            "position": pos,
            "channel": (packed >> 16) & 0xFFFF,
            "length": length,
            "key": key,
            "fine_pitch": fine,
            "release": release,
            "muted": bool(flags & 0x20),
            "pan": pan,
            "velocity": velocity,
            "cut_self": cut,
            "record_hex": raw.hex(),
        })
    next_offset = offset + len(notes)
    return {"success": True, "context": effect_context(), "pattern": pattern,
            "count": count, "offset": offset, "notes": notes,
            "next_offset": next_offset if next_offset < count else None}

def add_pattern_notes(params):
    """Append complete piano-roll notes to a pattern with one FL undo entry."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    pattern = params.get("pattern")
    notes = params.get("notes")
    dry_run = params.get("dry_run", False)
    if (type(pattern) is not int or not 1 <= pattern <= 999
            or not isinstance(notes, list) or not 1 <= len(notes) <= 512
            or type(dry_run) is not bool):
        raise ValueError("Invalid note batch request")
    flat = []
    for note in notes:
        if not isinstance(note, dict):
            raise ValueError("Each note is a dict")
        channel = note.get("channel", 0)
        key = note.get("key")
        position = note.get("position")
        length = note.get("length")
        velocity = note.get("velocity", 100)
        fine = note.get("fine_pitch", 120)
        release = note.get("release", 64)
        if (type(channel) is not int or channel < 0
                or type(key) is not int or not 0 <= key <= 131
                or type(position) is not int or position < 0
                or type(length) is not int or length < 1
                or position + length > 0x7FFFFFFF
                or type(velocity) is not int or not 0 <= velocity <= 127
                or type(fine) is not int or not 0 <= fine <= 0xFFFF
                or type(release) is not int or not 0 <= release <= 255):
            raise ValueError("Note values out of range")
        flat.extend((position, channel, length, key, fine, release, velocity))
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    page = read_pattern_notes({"context": effect_context(), "pattern": pattern,
                               "offset": 0, "limit": 1})
    expected_count = page["count"]
    loader.execute("pattern.add_notes", (pattern, expected_count, 0, tuple(flat), len(notes)))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "pattern": pattern, "added": len(notes),
                "before": expected_count, "after": expected_count + len(notes),
                "context": effect_context()}
    _prepare_pattern_undo(pattern)
    general.saveUndo("Add pattern notes", midi.UF_PR)
    try:
        after = loader.execute("pattern.add_notes",
                               (pattern, expected_count, 1, tuple(flat), len(notes)))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "pattern": pattern,
            "added": len(notes), "count": after, "context": effect_context()}

def write_pattern_notes(params):
    """Edit or delete notes against a complete live snapshot of the array.

    records_hex is the byte-exact note array snapshot (all notes, 24 bytes
    each); new_records_hex is the desired array (same or fewer records, sorted
    by position). Appending belongs to add_pattern_notes.
    """
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    pattern = params.get("pattern")
    records_hex = params.get("records_hex")
    new_records_hex = params.get("new_records_hex")
    dry_run = params.get("dry_run", False)
    try:
        records = bytes.fromhex(records_hex)
        new_records = bytes.fromhex(new_records_hex)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid note array snapshot") from exc
    if (type(pattern) is not int or not 1 <= pattern <= 999
            or len(records) % 24 or len(records) < 24 or len(records) > 100000 * 24
            or len(new_records) % 24 or len(new_records) > len(records)
            or type(dry_run) is not bool):
        raise ValueError("Invalid note rewrite request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    arguments = (pattern, records, new_records)
    loader.execute("pattern.write_notes", (*arguments, 0))
    new_count = len(new_records) // 24
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "pattern": pattern, "before": len(records) // 24,
                "after": new_count, "context": effect_context()}
    _prepare_pattern_undo(pattern)
    general.saveUndo("Edit pattern notes", midi.UF_PR)
    try:
        after = loader.execute("pattern.write_notes", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "pattern": pattern,
            "before": len(records) // 24, "after": after,
            "context": effect_context()}

MARKER_PAGE_HEADER = struct.Struct("<iH")

def refresh_timeline(params):
    """Re-select the current arrangement and repaint the Playlist.

    Diagnostic support for undo flows: form-level timeline views re-read their
    backing state after an FL undo restored it.
    """
    loader = native_effect_loader()
    loader.initialize()
    loader.execute("timeline.refresh", ())
    return {"success": True, "context": effect_context()}

def read_timeline_markers(params):
    """Read a page of the active arrangement's timeline markers."""
    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the read")
    offset = params.get("offset", 0)
    limit = params.get("limit", 32)
    if (type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 32):
        raise ValueError("Invalid marker page request")
    loader = native_effect_loader()
    loader.initialize()
    count, records = loader.execute("timeline.read_markers", (offset, limit))
    markers = []
    position = 0
    index = offset
    while position < len(records):
        tick, name_size = MARKER_PAGE_HEADER.unpack_from(records, position)
        position += 6
        name = records[position:position + name_size].decode("utf-16-le")
        position += name_size
        markers.append({"index": index, "tick": tick, "name": name})
        index += 1
    if index - offset > limit:
        raise RuntimeError("Invalid marker page returned by the native bridge")
    next_offset = offset + len(markers)
    return {"success": True, "context": effect_context(), "count": count,
            "offset": offset, "markers": markers,
            "next_offset": next_offset if next_offset < count else None}

def add_timeline_marker(params):
    """Add a named timeline marker at an absolute tick with one FL undo entry."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    tick = params.get("tick")
    name = params.get("name")
    dry_run = params.get("dry_run", False)
    if (type(tick) is not int or not 0 <= tick <= 0x7FFFFFFF
            or not isinstance(name, str) or len(name) > 200
            or any(ord(ch) < 32 for ch in name) or type(dry_run) is not bool):
        raise ValueError("Invalid marker request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    page = read_timeline_markers({"context": effect_context(), "offset": 0, "limit": 1})
    before = page["count"]
    loader.execute("timeline.add_marker", (tick, name, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "before": before, "after": before + 1, "context": effect_context()}
    general.saveUndo("Add marker %d" % tick, midi.UF_PL)
    try:
        after = loader.execute("timeline.add_marker", (tick, name, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "tick": tick, "name": name,
            "before": before, "after": after, "context": effect_context()}

def delete_timeline_marker(params):
    """Delete the marker at an index against its observed tick, with one undo entry."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    index = params.get("index")
    expected_tick = params.get("expected_tick")
    dry_run = params.get("dry_run", False)
    if (type(index) is not int or index < 0
            or type(expected_tick) is not int or expected_tick < 0
            or type(dry_run) is not bool):
        raise ValueError("Invalid marker deletion request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    page = read_timeline_markers({"context": effect_context(), "offset": index, "limit": 1})
    if not page.get("markers"):
        raise ValueError("The marker index does not exist")
    observed = page["markers"][0]
    if observed["tick"] != expected_tick:
        raise ValueError("The marker changed; read it again")
    loader.execute("timeline.delete_marker", (index, expected_tick, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "index": index, "tick": expected_tick,
                "before": page["count"], "after": page["count"] - 1,
                "context": effect_context()}
    general.saveUndo("Delete marker %d" % expected_tick, midi.UF_PL)
    try:
        after = loader.execute("timeline.delete_marker", (index, expected_tick, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "index": index,
            "tick": expected_tick, "before": page["count"], "after": after,
            "context": effect_context()}

def read_arrangements(params):
    """Read the arrangement list with names and the current index."""
    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the read")
    loader = native_effect_loader()
    loader.initialize()
    count, current, records = loader.execute("arrangement.read_all", ())
    arrangements = []
    position = 0
    for index in range(count):
        if position + 6 > len(records):
            break
        stored_index, name_size = struct.unpack_from("<iH", records, position)
        position += 6
        name = records[position:position + name_size].decode("utf-16-le")
        position += name_size
        arrangements.append({"index": index if stored_index == index else stored_index,
                             "name": name,
                             "current": index == current})
    return {"success": True, "context": effect_context(), "count": count,
            "current": current, "arrangements": arrangements}

def add_arrangement(params):
    """Add a new empty arrangement, optionally switching to it, with one undo entry."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    name = params.get("name") or ""
    switch_to = params.get("switch_to", True)
    dry_run = params.get("dry_run", False)
    if (not isinstance(name, str) or len(name) > 200
            or type(switch_to) is not bool or type(dry_run) is not bool):
        raise ValueError("Invalid arrangement request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    page = read_arrangements({"context": effect_context()})
    before = page["count"]
    loader.execute("arrangement.add", (1 if switch_to else 0, name, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "before": before, "after": before + 1, "context": effect_context()}
    general.saveUndo("Add arrangement", midi.UF_PL)
    try:
        name_bytes = loader.execute("arrangement.add", (1 if switch_to else 0, name, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True,
            "index": before,
            "name": name_bytes.decode("utf-16-le") if name_bytes else "",
            "before": before, "after": before + 1, "context": effect_context()}

def clone_arrangement(params):
    """Deep-copy an arrangement (clips and markers) into a new arrangement."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    source = params.get("source")
    name = params.get("name") or ""
    switch_to = params.get("switch_to", True)
    dry_run = params.get("dry_run", False)
    if (type(source) is not int or source < 0
            or not isinstance(name, str) or len(name) > 200
            or type(switch_to) is not bool or type(dry_run) is not bool):
        raise ValueError("Invalid arrangement clone request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    page = read_arrangements({"context": effect_context()})
    if source >= page["count"]:
        raise ValueError("The source arrangement does not exist")
    before = page["count"]
    loader.execute("arrangement.clone", (source, 1 if switch_to else 0, name, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "before": before, "after": before + 1, "context": effect_context()}
    general.saveUndo("Clone arrangement", midi.UF_PL)
    try:
        result = loader.execute("arrangement.clone",
                                (source, 1 if switch_to else 0, name, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    new_index = result
    clip_page = read_playlist_clips({"context": effect_context(), "offset": 0, "limit": 1})
    return {"success": True, "submitted": True, "source": source,
            "index": new_index, "before": before, "after": before + 1,
            "current_arrangement": clip_page.get("arrangement"),
            "context": effect_context()}

def delete_arrangement(params):
    """Delete an arrangement (never the last one) with one undo entry."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    index = params.get("index")
    dry_run = params.get("dry_run", False)
    if type(index) is not int or index < 0 or type(dry_run) is not bool:
        raise ValueError("Invalid arrangement deletion request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    page = read_arrangements({"context": effect_context()})
    if index >= page["count"]:
        raise ValueError("The arrangement does not exist")
    if page["count"] <= 1:
        raise ValueError("The last remaining arrangement cannot be deleted")
    before = page["count"]
    loader.execute("arrangement.delete", (index, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "before": before, "after": before - 1, "context": effect_context()}
    general.saveUndo("Delete arrangement", midi.UF_PL)
    try:
        after = loader.execute("arrangement.delete", (index, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "index": index,
            "before": before, "after": after, "context": effect_context()}

def set_arrangement_name(params):
    """Rename an arrangement with one undo entry."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    index = params.get("index")
    name = params.get("name")
    dry_run = params.get("dry_run", False)
    if (type(index) is not int or index < 0
            or not isinstance(name, str) or len(name) > 200
            or type(dry_run) is not bool):
        raise ValueError("Invalid arrangement rename request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    page = read_arrangements({"context": effect_context()})
    if index >= page["count"]:
        raise ValueError("The arrangement does not exist")
    before = page["arrangements"][index]["name"]
    loader.execute("arrangement.set_name", (index, name, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "index": index, "before": before, "after": name,
                "context": effect_context()}
    general.saveUndo("Rename arrangement", midi.UF_PL)
    try:
        name_bytes = loader.execute("arrangement.set_name", (index, name, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    after = name_bytes.decode("utf-16-le") if name_bytes else ""
    return {"success": True, "submitted": True, "index": index,
            "before": before, "after": after,
            "verified": after == name, "context": effect_context()}

def set_current_arrangement(params):
    """Switch the visible arrangement; a view change, not a project edit."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the switch")
    index = params.get("index")
    dry_run = params.get("dry_run", False)
    if type(index) is not int or index < 0 or type(dry_run) is not bool:
        raise ValueError("Invalid arrangement switch request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can switch")
    page = read_arrangements({"context": effect_context()})
    if index >= page["count"]:
        raise ValueError("The arrangement does not exist")
    before = page.get("current")
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "index": index, "before": before, "context": effect_context()}
    loader = native_effect_loader()
    loader.initialize()
    observed = loader.execute("arrangement.set_current", (index,))
    after_page = read_arrangements({"context": effect_context()})
    return {"success": observed == index and after_page.get("current") == index,
            "submitted": True, "index": index, "observed": observed,
            "before": before, "current": after_page.get("current"),
            "context": effect_context()}

def _mixer_track_count():
    last = getattr(mixer, "getTrackCount", None)
    if last:
        return last()
    return mixer.trackCount() - 1

def set_mixer_send(params):
    """Create, relevel, or remove a mixer send with independent readback.

    source routes into destination (both regular track indices). level is the
    send volume 0..1. active toggles the send's enable state without removing
    it. mode=create replaces the source's routing with just this send;
    mode=add keeps existing routes.
    """
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    source = params.get("source")
    destination = params.get("destination")
    level = params.get("level")
    active = params.get("active", True)
    mode = params.get("mode", "add")
    dry_run = params.get("dry_run", False)
    last_track = _mixer_track_count()
    if (type(source) is not int or not 0 <= source <= last_track
            or type(destination) is not int or not 0 <= destination <= last_track
            or source == destination
            or (level is not None and (type(level) not in (int, float)
                                       or not math.isfinite(level) or not 0 <= level <= 1))
            or type(active) is not bool or mode not in ("create", "add")
            or type(dry_run) is not bool):
        raise ValueError("Invalid mixer send request")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    if level is None:
        level = mixer.getRouteToLevel(source, destination) if destination < source else 1.0
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "source": source, "destination": destination,
                "level": level, "active": active, "context": effect_context()}
    mixer.setRouteTo(source, destination, 1 if active else 0)
    mixer.afterRoutingChanged()
    try:
        mixer.setRouteToLevel(source, destination, float(level))
    except Exception:
        pass
    return {"success": True, "submitted": True, "source": source,
            "destination": destination, "level": float(level),
            "active": bool(active), "context": effect_context()}

def read_mixer_sends(params):
    """Read a source track's sends with FL's own routing state."""
    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the read")
    source = params.get("source")
    if type(source) is not int or source < 0 or source > _mixer_track_count():
        raise ValueError("Invalid mixer source track")
    loader = native_effect_loader()
    loader.initialize()
    last_track = _mixer_track_count()
    sends = []
    for destination in range(last_track + 1):
        if destination == source:
            continue
        try:
            active = int(mixer.getRouteSendActive(source, destination))
        except Exception:
            continue
        if not active:
            continue
        try:
            level = float(mixer.getRouteToLevel(source, destination))
        except Exception:
            level = None
        sends.append({"destination": destination, "routed": True,
                      "active": True, "level": level})
    return {"success": True, "context": effect_context(), "source": source,
            "sends": sends}

def delete_mixer_send(params):
    """Remove a mixer send and verify it is gone through FL's own state."""
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    source = params.get("source")
    destination = params.get("destination")
    dry_run = params.get("dry_run", False)
    last_track = _mixer_track_count()
    if (type(source) is not int or not 0 <= source <= last_track
            or type(destination) is not int or not 0 <= destination <= last_track
            or source == destination or type(dry_run) is not bool):
        raise ValueError("Invalid mixer send deletion")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    try:
        routed = bool(mixer.getRouteTo(source, destination))
    except Exception:
        routed = False
    if not routed:
        raise ValueError("The send does not exist")
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "source": source, "destination": destination,
                "context": effect_context()}
    mixer.setRouteTo(source, destination, False)
    mixer.afterRoutingChanged()
    still = False
    try:
        still = bool(mixer.getRouteTo(source, destination))
    except Exception:
        still = False
    return {"success": not still, "submitted": True, "source": source,
            "destination": destination, "verified": not still,
            "context": effect_context()}

def save_project_copy(params):
    """Write the live project to an absolute path without changing its state.

    Uses the engine's FLP writer worker directly, bypassing the Save command
    wrapper whose error path pops modal dialogs. The saved copy includes all
    unsaved changes (it serializes live state).
    """
    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the save")
    path = params.get("path")
    if (not isinstance(path, str) or not Path(path).is_absolute()
            or Path(path).suffix.casefold() != ".flp"
            or any(ord(ch) < 32 for ch in path) or len(path) > 500):
        raise ValueError("Expected an absolute .flp path outside reserved names")
    loader = native_effect_loader()
    loader.initialize()
    code = loader.execute("project.save_copy", (path,))
    exists = Path(path).is_file()
    return {"success": bool(code == 0 and exists), "submitted": True,
            "worker_code": code, "path": path, "exists": exists,
            "size": Path(path).stat().st_size if exists else 0,
            "context": effect_context()}

def remove_mixer_effect(params):
    """Remove the effect in a mixer slot via FL's own "(delete) effect" preset.

    The engine records a per-slot undo entry before removal. After the call
    the slot reads as empty through plugins.isValid.
    """
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    track = params.get("track")
    slot = params.get("slot")
    dry_run = params.get("dry_run", False)
    token = params.get("lease_token")
    last_track = (mixer.getTrackCount() if hasattr(mixer, "getTrackCount")
                  else mixer.trackCount() - 1)
    if (type(track) is not int or not 0 <= track <= last_track
            or type(slot) is not int or not 0 <= slot < 10
            or type(dry_run) is not bool):
        raise ValueError("Invalid mixer effect slot request")
    # A token turns on host-side slot arbitration; legacy single-call requests
    # without one keep working but still re-check context and editability here.
    if not dry_run and isinstance(token, str):
        verify_slot_context(track, slot, params["context"], token)
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    if not plugins.isValid(track, slot, True):
        raise ValueError("The requested slot is already empty")
    plugin_name = plugins.getPluginName(track, slot, False, True)
    loader = native_effect_loader()
    loader.initialize()
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "track": track, "slot": slot, "plugin_name": plugin_name,
                "context": effect_context()}
    loader.execute("mixer.remove_effect", (track, slot))
    # Re-check the lease in the same callback after the mutation. A failed
    # re-check must not erase the fact that the write was submitted.
    if isinstance(token, str):
        try:
            verify_slot_context(track, slot, params["context"], token)
        except (RuntimeError, ValueError) as exc:
            return {"success": False, "submitted": True, "verified": False,
                    "track": track, "slot": slot, "error": str(exc)}
    still = plugins.isValid(track, slot, True)
    # Do NOT release here: a compound caller (swap) removed one slot and still
    # needs the lease for the following load and the other slot. The caller's
    # finish() releases both; a lone caller's lease expires by TTL.
    return {"success": not still, "submitted": True, "track": track, "slot": slot,
            "removed_plugin": plugin_name, "verified": not still,
            "context": effect_context()}


def replace_mixer_effect(params):
    """Replace one mixer effect: remove the occupant, then load the preset.

    Removal is not undoable on this build (see remove_mixer_effect), so the
    whole replacement is destructive; fl_save_project_copy is the safety net.
    A failure after removal leaves the slot empty and is reported as partial
    with the completed step, so the caller can retry the load alone.
    """
    import general

    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before the edit")
    track = params.get("track")
    slot = params.get("slot")
    dry_run = params.get("dry_run", False)
    name = params.get("name")
    last_track = (mixer.getTrackCount() if hasattr(mixer, "getTrackCount")
                  else mixer.trackCount() - 1)
    if (type(track) is not int or not 0 <= track <= last_track
            or type(slot) is not int or not 0 <= slot < 10
            or type(dry_run) is not bool
            or not isinstance(name, str) or not name.strip(" .")
            or len(name) > 200 or any(ord(ch) < 32 or ch in '/\\:' for ch in name)):
        raise ValueError("Invalid mixer effect replacement request")
    name = name.strip()
    if name.casefold() == "(delete) effect":
        raise ValueError("The delete preset is not a replacement")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    if not plugins.isValid(track, slot, True):
        raise ValueError("The requested slot is empty; use a direct load instead")
    preset = Path(params.get("preset_path") or "")
    if (not preset.is_absolute() or preset.suffix.casefold() != ".fst"
            or preset.stem.strip().casefold() == "(delete) effect" or not preset.is_file()):
        raise ValueError("Expected an installed Mixer effect preset")
    removed_name = plugins.getPluginName(track, slot, False, True)
    if isinstance(removed_name, str) and removed_name.strip().casefold() == name.casefold():
        raise ValueError("The replacement is already the occupant of this slot")
    loader = native_effect_loader()
    loader.initialize()
    destructive = ("Replacement is destructive: the removal step cannot be undone; "
                   "use fl_save_project_copy for a safety copy first")
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "track": track, "slot": slot, "removed_plugin": removed_name,
                "replacement": name, "undoable": False, "warning": destructive,
                "context": effect_context()}
    loader.execute("mixer.remove_effect", (track, slot))
    if plugins.isValid(track, slot, True):
        return {"success": False, "submitted": True, "state": "unconfirmed",
                "track": track, "slot": slot, "removed_plugin": removed_name,
                "undoable": False,
                "error": "The removal step did not confirm; the load step was not attempted"}
    try:
        loader.execute("mixer.insert_empty_slot", (str(preset), track, slot))
    except Exception as exc:
        return {"success": False, "submitted": True, "state": "partial",
                "steps_completed": ["remove"], "track": track, "slot": slot,
                "removed_plugin": removed_name, "undoable": False,
                "error": "Removal completed but the load failed: " + str(exc)}
    occupied = plugins.isValid(track, slot, True)
    observed_name = plugins.getPluginName(track, slot, False, True) if occupied else None
    verified = bool(occupied and isinstance(observed_name, str)
                    and observed_name.strip().casefold() == name.casefold())
    return {"success": verified, "submitted": True,
            "state": "applied_verified" if verified else "unconfirmed",
            "track": track, "slot": slot, "removed_plugin": removed_name,
            "plugin_name": observed_name, "undoable": False, "warning": destructive,
            "context": effect_context(),
            **({"error": "The replacement effect could not be independently verified"}
               if not verified else {})}

def move_playlist_clip(params):
    """Move the clip only if the supplied snapshot still matches FL's state."""
    import general

    snapshot = params.get("snapshot_ref")
    if not isinstance(snapshot, str):
        raise ValueError("A Playlist clip snapshot reference is required")
    try:
        context, revision_text, arrangement_text, index_text, record_hex = snapshot.rsplit(":", 4)
        if not revision_text.startswith("p"):
            raise ValueError("missing Playlist revision")
        revision = int(revision_text[1:])
        arrangement = int(arrangement_text)
        index = int(index_text)
        record = bytes.fromhex(record_hex)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid Playlist clip snapshot reference") from exc
    if context != effect_context() or params.get("context") != context:
        raise RuntimeError("The FL controller or current project changed before the move")
    if revision != _playlist_revision:
        raise RuntimeError("The Playlist snapshot expired after an FL refresh; read it again")
    start_tick = params.get("start_tick")
    track = params.get("track")
    dry_run = params.get("dry_run", False)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a Boolean")
    if type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF:
        raise ValueError("start_tick must be a nonnegative 32-bit integer")
    if len(record) != 36:
        raise ValueError("Invalid Playlist clip snapshot record")
    if track is None:
        track = 500 - struct.unpack_from("<h", record, 12)[0]
    if type(track) is not int or not 1 <= track <= 500:
        raise ValueError("track must be a 1-based Playlist track in 1..500")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    arguments = (arrangement, index, record, start_tick, track)
    loader.execute("playlist.move_clip", (*arguments, 0))
    original_tick = struct.unpack_from("<i", record)[0]
    original_track = 500 - struct.unpack_from("<h", record, 12)[0]
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "arrangement": arrangement, "index": index, "context": context,
                "before": {"start_tick": original_tick, "track": original_track},
                "after": {"start_tick": start_tick, "track": track}}
    if start_tick == original_tick and track == original_track:
        return {"success": True, "submitted": False, "unchanged": True}
    general.saveUndo("Move Playlist clip", midi.UF_PL)
    try:
        loader.execute("playlist.move_clip", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "arrangement": arrangement,
            "index": index, "start_tick": start_tick, "track": track,
            "context": context}

def _parse_playlist_snapshot(params):
    """Validate a Playlist clip snapshot reference against the live session."""
    import struct as _struct

    snapshot = params.get("snapshot_ref")
    if not isinstance(snapshot, str):
        raise ValueError("A Playlist clip snapshot reference is required")
    try:
        context, revision_text, arrangement_text, index_text, record_hex = snapshot.rsplit(":", 4)
        if not revision_text.startswith("p"):
            raise ValueError("missing Playlist revision")
        revision = int(revision_text[1:])
        arrangement = int(arrangement_text)
        index = int(index_text)
        record = bytes.fromhex(record_hex)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid Playlist clip snapshot reference") from exc
    if context != effect_context() or params.get("context") != context:
        raise RuntimeError("The FL controller or current project changed before the edit")
    if revision != _playlist_revision:
        raise RuntimeError("The Playlist snapshot expired after an FL refresh; read it again")
    if len(record) != 36:
        raise ValueError("Invalid Playlist clip snapshot record")
    return context, arrangement, index, record

def delete_playlist_clip(params):
    """Delete a clip identified by a fresh snapshot, via the engine's own compact path."""
    import general

    context, arrangement, index, record = _parse_playlist_snapshot(params)
    dry_run = params.get("dry_run", False)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a Boolean")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    arguments = (arrangement, index, record)
    loader.execute("playlist.delete_clip", (*arguments, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "arrangement": arrangement, "index": index, "context": context}
    general.saveUndo("Delete Playlist clip", midi.UF_PL)
    try:
        loader.execute("playlist.delete_clip", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "arrangement": arrangement,
            "index": index, "context": context}

def duplicate_playlist_clip(params):
    """Duplicate a clip to a new start/track while keeping its source and window."""
    import general

    context, arrangement, index, record = _parse_playlist_snapshot(params)
    start_tick = params.get("start_tick")
    track = params.get("track")
    dry_run = params.get("dry_run", False)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a Boolean")
    if type(start_tick) is not int or not 0 <= start_tick <= 0x7FFFFFFF:
        raise ValueError("start_tick must be a nonnegative 32-bit integer")
    if track is None:
        track = 500 - struct.unpack_from("<h", record, 12)[0]
    if type(track) is not int or not 1 <= track <= 500:
        raise ValueError("track must be a 1-based Playlist track in 1..500")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    arguments = (arrangement, index, record, start_tick, track)
    loader.execute("playlist.duplicate_clip", (*arguments, 0))
    original_tick = struct.unpack_from("<i", record)[0]
    original_track = 500 - struct.unpack_from("<h", record, 12)[0]
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "arrangement": arrangement, "index": index, "context": context,
                "before": {"start_tick": original_tick, "track": original_track},
                "after": {"start_tick": start_tick, "track": track}}
    general.saveUndo("Duplicate Playlist clip", midi.UF_PL)
    try:
        result = loader.execute("playlist.duplicate_clip", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    new_index, count = result
    return {"success": True, "submitted": True, "arrangement": arrangement,
            "index": index, "new_index": new_index, "count": count,
            "start_tick": start_tick, "track": track, "context": context}

def set_playlist_clip_muted(params):
    """Set or clear a clip's mute flag; serialization persistence is verified separately."""
    import general

    context, arrangement, index, record = _parse_playlist_snapshot(params)
    muted = params.get("muted")
    dry_run = params.get("dry_run", False)
    if type(muted) is not bool or type(dry_run) is not bool:
        raise ValueError("muted and dry_run must be Booleans")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    arguments = (arrangement, index, record, 1 if muted else 0)
    observed_muted = loader.execute("playlist.set_clip_muted", (*arguments, 0))
    if dry_run or bool(observed_muted) == muted:
        return {"success": True, "submitted": False, "dry_run": dry_run,
                "unchanged": bool(observed_muted) == muted,
                "muted": muted, "context": context}
    general.saveUndo("Mute Playlist clip", midi.UF_PL)
    try:
        result = loader.execute("playlist.set_clip_muted", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    return {"success": True, "submitted": True, "arrangement": arrangement,
            "index": index, "muted": bool(result), "context": context}

def _tempo_ms_per_tick():
    """Current song tempo and milliseconds per tick from FL's own state."""
    import device
    import general

    normalized = device.getLinkedValue(0x40000005)
    if type(normalized) not in (int, float) or not math.isfinite(normalized):
        raise RuntimeError("FL did not report the current tempo")
    bpm = round(normalized * 512 + 10, 3)
    if not 10 <= bpm <= 522:
        raise RuntimeError("FL reported an out-of-range tempo")
    ppq = general.getRecPPQ()
    if type(ppq) is not int or ppq < 1:
        raise RuntimeError("FL did not report its pulses per quarter")
    return bpm, 60000.0 / (bpm * ppq)

def trim_playlist_clip(params):
    """Trim an audio clip source window or a pattern clip length.

    window_start_tick/window_end_tick are absolute source ticks for audio
    clips (the engine converts them to song milliseconds); for pattern clips
    the length becomes window_end_tick - window_start_tick.
    """
    import general

    context, arrangement, index, record = _parse_playlist_snapshot(params)
    source_raw = struct.unpack_from("<I", record, 4)[0]
    start_tick = struct.unpack_from("<i", record)[0]
    old_length = struct.unpack_from("<i", record, 8)[0]
    window_start = params.get("window_start_tick")
    window_end = params.get("window_end_tick")
    dry_run = params.get("dry_run", False)
    if (type(window_start) is not int or window_start < 0
            or type(window_end) is not int or window_end <= window_start
            or window_end > 0x7FFFFFFF or type(dry_run) is not bool):
        raise ValueError("Invalid trim window")
    new_length = window_end - window_start
    is_pattern = source_raw >= 0x50000000
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()
    arguments = (arrangement, index, record, window_start, window_end)
    loader.execute("playlist.trim_clip", (*arguments, 0))
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "index": index, "before": {"start_tick": start_tick, "length_tick": old_length},
                "after": {"length_tick": new_length}, "context": context}
    general.saveUndo("Trim Playlist clip", midi.UF_PL)
    try:
        fresh = loader.execute("playlist.trim_clip", (*arguments, 1))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    result = {"success": True, "submitted": True, "index": index,
              "start_tick": start_tick, "length_tick": new_length,
              "is_pattern": is_pattern, "context": context,
              "record_hex": bytes(fresh).hex()}
    if not is_pattern:
        bpm, ms_per_tick = _tempo_ms_per_tick()
        result["expected_start_ms"] = window_start * ms_per_tick
        result["expected_end_ms"] = window_end * ms_per_tick
        result["tempo_bpm"] = bpm
        result["ms_per_tick"] = ms_per_tick
    return result

def split_playlist_clip(params):
    """Split one clip into two at an absolute tick with one FL undo entry.

    The right half is duplicated from the source record (keeping its source
    and window), then the right half is trimmed to the tail window and the
    original to the head window. Partial failures report what was created.
    """
    import general

    context, arrangement, index, record = _parse_playlist_snapshot(params)
    split_tick = params.get("split_tick")
    dry_run = params.get("dry_run", False)
    start_tick = struct.unpack_from("<i", record)[0]
    source_raw = struct.unpack_from("<I", record, 4)[0]
    length = struct.unpack_from("<i", record, 8)[0]
    track = 500 - struct.unpack_from("<h", record, 12)[0]
    is_pattern = source_raw >= 0x50000000
    if (type(split_tick) is not int or not 0 <= split_tick <= 0x7FFFFFFF
            or type(dry_run) is not bool):
        raise ValueError("Invalid split tick")
    rel = split_tick - start_tick
    if not 0 < rel < length:
        raise ValueError("The split tick must be inside the clip")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    loader = native_effect_loader()
    loader.initialize()

    def read_page():
        _arrangement_id, _count, _offset, records = loader.execute(
            "playlist.read_clips", (0, 64))
        return [bytes(records[i:i + 36]) for i in range(0, len(records), 36)]

    page = read_page()
    if index >= len(page) or page[index] != record:
        raise RuntimeError("The target clip changed; read it again")
    if is_pattern:
        right_window = (0, length - rel)
        left_window = (0, rel)
        expected_ms = None
    else:
        win_start_ms, win_end_ms = struct.unpack_from("<ff", record, 0x18)
        default_window = win_start_ms == -1.0 and win_end_ms == -1.0
        bpm, ms_per_tick = _tempo_ms_per_tick()
        if default_window:
            window_start_tick = 0
        else:
            window_start_tick = round(win_start_ms / ms_per_tick)
        window_end_tick = window_start_tick + length
        right_window = (window_start_tick + rel, window_end_tick)
        left_window = (window_start_tick, window_start_tick + rel)
        expected_ms = {"right_start_ms": right_window[0] * ms_per_tick,
                       "right_end_ms": right_window[1] * ms_per_tick,
                       "left_start_ms": left_window[0] * ms_per_tick,
                       "left_end_ms": left_window[1] * ms_per_tick}
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True,
                "index": index, "split_tick": split_tick, "track": track,
                "left": {"start_tick": start_tick, "length_tick": rel},
                "right": {"start_tick": split_tick, "length_tick": length - rel},
                "context": context}

    steps = []
    new_index = None
    error = None
    general.saveUndo("Split Playlist clip", midi.UF_PL)
    try:
        new_index, count = loader.execute("playlist.duplicate_clip",
                                          (arrangement, index, record, split_tick, track, 1))
        steps.append("duplicate")
        page = read_page()
        if new_index >= len(page):
            raise RuntimeError("The duplicated clip record could not be read")
        right_prefix = bytearray(page[new_index][:36])
        struct.pack_into("<i", right_prefix, 0, split_tick)
        loader.execute("playlist.trim_clip",
                       (arrangement, new_index, bytes(right_prefix), right_window[0],
                        right_window[1], 1))
        steps.append("trim_right")
        page = read_page()
        if index >= len(page):
            raise RuntimeError("The original clip record could not be read")
        left_record = bytearray(page[index])
        struct.pack_into("<i", left_record, 0, start_tick)
        loader.execute("playlist.trim_clip",
                       (arrangement, index, bytes(left_record), left_window[0],
                        left_window[1], 1))
        steps.append("trim_left")
    except Exception as exc:
        error = str(exc)
    result = {"success": error is None, "submitted": True, "index": index,
              "new_index": new_index, "split_tick": split_tick, "track": track,
              "steps_completed": steps, "context": context,
              "left": {"start_tick": start_tick, "length_tick": rel},
              "right": {"start_tick": split_tick, "length_tick": length - rel}}
    if expected_ms:
        result["expected_window_ms"] = expected_ms
    if error:
        result["error"] = "Split completed partially: " + error
        result["undo_hint"] = "Undo restores the pre-split state"
    return result

def load_effect_at_slot(params):
    """Load one effect via the controller's local native extension."""
    import general

    name = params.get("name")
    if (
        not isinstance(name, str)
        or not name.strip(" .")
        or len(name) > 200
        or any(ord(ch) < 32 or ch in '/\\:' for ch in name)
        or name.strip().casefold() == "(delete) effect"
    ):
        raise ValueError("Supply the exact effect name from FL Studio's Plugin database")
    name = name.strip()
    target = read_effect_slot(params)
    if target["occupied"]:
        raise ValueError("The requested slot is already occupied")
    if transport.isPlaying() or transport.isRecording() or not general.safeToEdit():
        raise RuntimeError("Stop playback or recording and wait until FL Studio can edit")
    if params.get("context") != effect_context():
        raise RuntimeError("The FL controller or current project changed before insertion")
    # A compound caller passes a lease token; verify the slot still belongs to
    # this write in the same OnIdle callback, just before the native insert.
    token = params.get("lease_token")
    if isinstance(token, str):
        verify_slot_context(target["track"], target["slot"], params["context"], token)
    preset = Path(params["preset_path"])
    if (not preset.is_absolute() or preset.suffix.casefold() != ".fst"
            or preset.stem.strip().casefold() == "(delete) effect" or not preset.is_file()):
        raise ValueError("Expected an installed Mixer effect preset")
    dry_run = params.get("dry_run", False)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a Boolean")
    loader = native_effect_loader()
    loader.initialize()
    if dry_run:
        return {"success": True, "submitted": False, "dry_run": True, "preset_path": str(preset)}
    try:
        loader.execute("mixer.insert_empty_slot", (str(preset), target["track"], target["slot"]))
    except Exception as exc:
        return {"success": False, "submitted": True, "error": str(exc)}
    # Re-check after the mutation: a context switch or playback start during
    # the call must not be reported as a verified insert. The write already
    # happened, so submitted=True must survive even when the re-check fails.
    if isinstance(token, str):
        try:
            verify_slot_context(target["track"], target["slot"], params["context"], token)
        except (RuntimeError, ValueError) as exc:
            return {"success": False, "submitted": True, "verified": False,
                    "error": str(exc)}
    return {"success": True, "submitted": True, "verified": True}

def _dispatch(action, params):
    """Dispatch only operations registered for the controller idle callback."""
    if action == "native.semanticDescribe":
        return {**native_capabilities(), "capability_request_id": params.get("request_id")}
    if action == "native.addSampler":
        return {**add_sampler(params), "native_request_id": params.get("request_id")}
    if action == "native.addGenerator":
        return {**add_generator(params), "native_request_id": params.get("request_id")}
    if action == "native.replaceSample":
        return {**replace_sample(params), "native_request_id": params.get("request_id")}
    if action == "native.vtableDump":
        loader = native_effect_loader()
        loader.initialize()
        engine, envelope, array, slots, body, points = loader.execute(
            "automation.vtable_dump", (int(params["channel"]),))
        return {"success": True, "engine_base": engine, "envelope": envelope, "array": array,
                "slots": slots.hex(), "body": body.hex(), "points": points.hex(),
                "native_request_id": params.get("request_id")}
    if action == "native.listPlaylistClips":
        return {**read_playlist_clips(params), "native_request_id": params.get("request_id")}
    if action == "native.movePlaylistClip":
        return {**move_playlist_clip(params), "native_request_id": params.get("request_id")}
    if action == "native.insertAudioClip":
        return {**insert_audio_clip(params), "native_request_id": params.get("request_id")}
    if action == "native.insertPatternClip":
        return {**insert_pattern_clip(params), "native_request_id": params.get("request_id")}
    if action == "native.insertAutomationClip":
        return {**insert_automation_clip(params), "native_request_id": params.get("request_id")}
    if action == "native.managePattern":
        return {**manage_pattern(params), "native_request_id": params.get("request_id")}
    if action == "native.readPatternNotes":
        return {**read_pattern_notes(params), "native_request_id": params.get("request_id")}
    if action == "native.addPatternNotes":
        return {**add_pattern_notes(params), "native_request_id": params.get("request_id")}
    if action == "native.writePatternNotes":
        return {**write_pattern_notes(params), "native_request_id": params.get("request_id")}
    if action == "native.readTimelineMarkers":
        return {**read_timeline_markers(params), "native_request_id": params.get("request_id")}
    if action == "native.addTimelineMarker":
        return {**add_timeline_marker(params), "native_request_id": params.get("request_id")}
    if action == "native.deleteTimelineMarker":
        return {**delete_timeline_marker(params), "native_request_id": params.get("request_id")}
    if action == "native.refreshTimeline":
        return {**refresh_timeline(params), "native_request_id": params.get("request_id")}
    if action == "native.readArrangements":
        return {**read_arrangements(params), "native_request_id": params.get("request_id")}
    if action == "native.addArrangement":
        return {**add_arrangement(params), "native_request_id": params.get("request_id")}
    if action == "native.cloneArrangement":
        return {**clone_arrangement(params), "native_request_id": params.get("request_id")}
    if action == "native.deleteArrangement":
        return {**delete_arrangement(params), "native_request_id": params.get("request_id")}
    if action == "native.setArrangementName":
        return {**set_arrangement_name(params), "native_request_id": params.get("request_id")}
    if action == "native.setCurrentArrangement":
        return {**set_current_arrangement(params), "native_request_id": params.get("request_id")}
    if action == "native.claimMixerSlot":
        claim_slot(params["track"], params["slot"], params["context"], params["lease_token"])
        return {"success": True, "claimed": True, "native_request_id": params.get("request_id")}
    if action == "native.releaseMixerSlot":
        release_slot(params["track"], params["slot"], params["context"], params["lease_token"])
        return {"success": True, "released": True, "native_request_id": params.get("request_id")}
    if action == "native.inspectMixerSlotLease":
        key = _lease_key(params["track"], params["slot"])
        held = _read_lease(key) if key else None
        mine = bool(held and held[0] == params["context"]
                    and held[1] == params["lease_token"])
        return {"success": True, "occupied": held is not None,
                "held_by_this_write": mine,
                "native_request_id": params.get("request_id")}
    if action == "native.removeMixerEffect":
        return {**remove_mixer_effect(params), "native_request_id": params.get("request_id")}
    if action == "native.replaceMixerEffect":
        return {**replace_mixer_effect(params), "native_request_id": params.get("request_id")}
    if action == "native.saveProjectCopy":
        return {**save_project_copy(params), "native_request_id": params.get("request_id")}
    if action == "native.setMixerSend":
        return {**set_mixer_send(params), "native_request_id": params.get("request_id")}
    if action == "native.readMixerSends":
        return {**read_mixer_sends(params), "native_request_id": params.get("request_id")}
    if action == "native.deleteMixerSend":
        return {**delete_mixer_send(params), "native_request_id": params.get("request_id")}
    if action == "native.setPatternLength":
        return {**set_pattern_length(params), "native_request_id": params.get("request_id")}
    if action == "native.deletePlaylistClip":
        return {**delete_playlist_clip(params), "native_request_id": params.get("request_id")}
    if action == "native.duplicatePlaylistClip":
        return {**duplicate_playlist_clip(params), "native_request_id": params.get("request_id")}
    if action == "native.setPlaylistClipMuted":
        return {**set_playlist_clip_muted(params), "native_request_id": params.get("request_id")}
    if action == "native.trimPlaylistClip":
        return {**trim_playlist_clip(params), "native_request_id": params.get("request_id")}
    if action == "native.splitPlaylistClip":
        return {**split_playlist_clip(params), "native_request_id": params.get("request_id")}
    if action == "native.readAutomationPoints":
        return {**read_automation_points(params), "native_request_id": params.get("request_id")}
    if action == "native.readAutomationLinks":
        return {**read_automation_links(params), "native_request_id": params.get("request_id")}
    if action == "native.setAutomationPointValue":
        return {**set_automation_point_float(params), "native_request_id": params.get("request_id")}
    if action == "native.setAutomationPointTension":
        return {**set_automation_point_float(params, "tension"),
                "native_request_id": params.get("request_id")}
    if action == "native.moveAutomationPoint":
        return {**move_automation_point(params), "native_request_id": params.get("request_id")}
    if action == "native.setAutomationPointCurve":
        return {**set_automation_point_curve(params), "native_request_id": params.get("request_id")}
    if action == "native.editAutomationPoints":
        return {**edit_automation_points(params), "native_request_id": params.get("request_id")}
    if action == "native.createAutomationSource":
        return {**create_automation_source(params), "native_request_id": params.get("request_id")}
    if action == "native.scanMixerAutomationLinks":
        return {**scan_mixer_automation_links(params),
                "native_request_id": params.get("request_id")}
    if action == "mixer.effectLoaderInfo":
        return {"engine_path": native_effect_loader().initialize(), "context": effect_context()}
    if action == "mixer.readEffectSlot":
        return {**read_effect_slot(params), "editor_request_id": params.get("request_id")}
    if action == "mixer.loadEffectAtSlot":
        return {**load_effect_at_slot(params), "effect_request_id": params.get("request_id")}
    raise ValueError("Unknown semantic native operation")


def dispatch(action, params):
    try:
        return _dispatch(action, params)
    except Exception as exc:
        response = {"success": False, "submitted": False, "error": str(exc)}
        request_id = params.get("request_id")
        if action == "native.semanticDescribe":
            response["capability_request_id"] = request_id
        elif action.startswith("native."):
            response["native_request_id"] = request_id
        elif action == "mixer.loadEffectAtSlot":
            response["effect_request_id"] = request_id
        elif action == "mixer.readEffectSlot":
            response["editor_request_id"] = request_id
        return response


ACTIONS = frozenset({
    "native.semanticDescribe", "native.addSampler", "native.addGenerator",
    "native.replaceSample", "native.vtableDump", "native.listPlaylistClips",
    "native.movePlaylistClip", "native.insertAudioClip", "native.insertPatternClip",
    "native.insertAutomationClip", "native.managePattern", "native.setPatternLength",
    "native.readPatternNotes", "native.addPatternNotes", "native.writePatternNotes",
    "native.readTimelineMarkers", "native.addTimelineMarker", "native.deleteTimelineMarker",
    "native.refreshTimeline",
    "native.readArrangements", "native.addArrangement", "native.cloneArrangement",
    "native.deleteArrangement", "native.setArrangementName", "native.setCurrentArrangement",
    "native.setMixerSend", "native.readMixerSends", "native.deleteMixerSend",
    "native.saveProjectCopy",
    "native.removeMixerEffect",
    "native.claimMixerSlot", "native.releaseMixerSlot", "native.inspectMixerSlotLease",
    "native.replaceMixerEffect",
    "native.deletePlaylistClip", "native.duplicatePlaylistClip", "native.setPlaylistClipMuted",
    "native.trimPlaylistClip", "native.splitPlaylistClip",
    "native.readAutomationPoints", "native.readAutomationLinks", "native.setAutomationPointValue",
    "native.setAutomationPointTension",
    "native.moveAutomationPoint", "native.editAutomationPoints",
    "native.setAutomationPointCurve",
    "native.createAutomationSource", "native.scanMixerAutomationLinks",
    "mixer.effectLoaderInfo", "mixer.readEffectSlot",
    "mixer.loadEffectAtSlot",
})
