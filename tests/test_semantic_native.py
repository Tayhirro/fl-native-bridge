import importlib.util
import struct
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def semantic(monkeypatch):
    rows = []
    for index in range(2):
        row = bytearray(36)
        struct.pack_into("<iIih", row, 0, index * 384, 0x50095000, 384, 499 - index)
        rows.append(row)
    undo = []
    points = [bytearray(struct.pack("<fffB19x", 0.0, 0.25, 0.0, 0)),
              bytearray(struct.pack("<fffB19x", 2.0, 0.75, -0.5, 1))]

    def execute(operation, args):
        if operation == "playlist.read_clips":
            offset, limit = args
            return 0, len(rows), offset, b"".join(rows[offset:offset + limit])
        if operation == "playlist.move_clip":
            arrangement, index, expected, tick, track, commit = args
            if arrangement != 0 or index >= len(rows) or rows[index] != expected:
                raise ValueError("The Playlist clip changed; read it again")
            if any(row == expected for i, row in enumerate(rows) if i != index):
                raise ValueError("Ambiguous Playlist snapshot: identical clips exist")
            if commit:
                struct.pack_into("<i", rows[index], 0, tick)
                struct.pack_into("<h", rows[index], 12, 500 - track)
            return 0
        if operation == "automation.read_points":
            channel, offset, limit = args
            if channel != 5:
                raise ValueError("Channel is not a supported Automation Clip")
            prior = sum((struct.unpack_from("<f", point)[0] for point in points[:offset]), 0.0)
            return 5, 0x50000, 2, 100.0, prior, b"".join(points[offset:offset + limit])
        if operation == "automation.read_links":
            channel, offset, limit = args
            if channel != 5:
                raise ValueError("Channel is not a supported Automation Clip")
            ids = [32822, 1904246790]
            return 5, 0x50000, len(ids), b"".join(
                struct.pack("<I", item) for item in ids[offset:offset + limit]
            )
        if operation in ("automation.set_point_value", "automation.set_point_tension"):
            channel, index, source, prior_time, expected, value, commit = args
            actual_prior = sum(struct.unpack_from("<f", point)[0] for point in points[:index])
            if (channel != 5 or source != 0x50000 or index >= len(points)
                    or prior_time != actual_prior or points[index] != expected):
                raise ValueError("Automation point changed; read it again")
            if commit:
                undo.append(("automation", bytes(points[index])))
                offset = 8 if operation.endswith("tension") else 4
                struct.pack_into("<f", points[index], offset, value)
            return commit
        if operation == "automation.move_point":
            channel, index, source, prior, expected, next_record, target, commit = args
            actual_prior = sum((struct.unpack_from("<f", p)[0] for p in points[:index]), 0.0)
            following = bytes(points[index + 1]) if index + 1 < len(points) else b""
            if (channel != 5 or source != 0x50000 or prior != actual_prior
                    or bytes(points[index]) != expected or following != next_record):
                raise ValueError("Automation point changed; read it again")
            end = (prior + struct.unpack_from("<f", expected)[0]
                   + (struct.unpack_from("<f", next_record)[0] if next_record else 0))
            if target < prior or (next_record and target > end):
                raise ValueError("Position would cross a point")
            if commit:
                undo.append(("automation", [bytes(p) for p in points]))
                struct.pack_into("<f", points[index], 0, target - prior)
                if next_record:
                    struct.pack_into("<f", points[index + 1], 0, end - target)
            return commit
        raise AssertionError(operation)

    modules = {
        "channels": {"channelCount": lambda *_: 2},
        "midi": {"UF_PL": 4},
        "mixer": {"trackCount": lambda: 9, "getEventIDName": lambda event: f"event {event}"},
        "plugins": {},
        "device": {"getLinkedValue": lambda event: 0.625 if event == 32822 else -1},
        "transport": {"isPlaying": lambda: False, "isRecording": lambda: False},
        "general": {"safeToEdit": lambda: True, "saveUndo": lambda *args: undo.append(args)},
        "_flmcp_native": {
            "initialize": lambda: "FLEngine_x64.dll",
            "describe": lambda: {"api_version": 1, "engine_profile": "26.1.7.5654-x64",
                                 "operations": ["playlist.read_clips", "playlist.move_clip",
                                                "automation.read_points"]},
            "execute": execute,
        },
    }
    for name, values in modules.items():
        module = ModuleType(name)
        vars(module).update(values)
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[1] / "fl_controller/semantic_native.py"
    spec = importlib.util.spec_from_file_location("semantic_native_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return SimpleNamespace(module=module, rows=rows, points=points, undo=undo)


def test_move_checks_snapshot_and_preserves_neighbor(semantic):
    module = semantic.module
    context = module.effect_context()
    before = module.dispatch("native.listPlaylistClips", {
        "context": context, "request_id": "read", "offset": 0, "limit": 2,
    })
    clip = before["clips"][0]
    neighbor = bytes(semantic.rows[1])
    params = {"context": context, "request_id": "move", "snapshot_ref": clip["snapshot_ref"],
              "start_tick": 1, "track": 2}
    moved = module.dispatch("native.movePlaylistClip", params)
    assert moved["submitted"] and moved["native_request_id"] == "move"
    assert struct.unpack_from("<i", semantic.rows[0])[0] == 1
    assert struct.unpack_from("<h", semantic.rows[0], 12)[0] == 498
    assert bytes(semantic.rows[1]) == neighbor and len(semantic.undo) == 1
    stale = module.dispatch("native.movePlaylistClip", {**params, "request_id": "stale"})
    assert not stale["success"] and not stale["submitted"] and len(semantic.undo) == 1


def test_project_change_invalidates_snapshot(semantic):
    module = semantic.module
    old_context = module.effect_context()
    page = module.dispatch("native.listPlaylistClips", {"context": old_context})
    module.project_changed()
    result = module.dispatch("native.movePlaylistClip", {
        "context": old_context, "snapshot_ref": page["clips"][0]["snapshot_ref"],
        "start_tick": 1,
    })
    assert not result["success"] and "project changed" in result["error"]
    assert not semantic.undo


def test_host_refresh_invalidates_only_playlist_snapshot(semantic):
    module = semantic.module
    context = module.effect_context()
    clip = module.dispatch("native.listPlaylistClips", {"context": context})["clips"][0]
    module.playlist_refreshed()
    result = module.dispatch("native.movePlaylistClip", {
        "context": context, "snapshot_ref": clip["snapshot_ref"], "start_tick": 1,
    })
    assert not result["success"] and not result["submitted"]
    assert "expired" in result["error"]
    assert module.effect_context() == context
    fresh = module.dispatch("native.listPlaylistClips", {"context": context})["clips"][0]
    assert module.dispatch("native.movePlaylistClip", {
        "context": context, "snapshot_ref": fresh["snapshot_ref"],
        "start_tick": 1, "dry_run": True,
    })["success"]


def test_sampler_partial_status_does_not_claim_source_verified(semantic, monkeypatch, tmp_path):
    module = semantic.module
    source = tmp_path / "sample.wav"
    source.write_bytes(b"fixture")
    state = {"count": 2, "writes": 0}
    channels = sys.modules["channels"]
    monkeypatch.setattr(channels, "channelCount", lambda *_: state["count"])
    monkeypatch.setattr(channels, "getChannelType", lambda *_: 0, raising=False)
    monkeypatch.setattr(channels, "getChannelName", lambda *_: "sample", raising=False)
    loader = module.native_effect_loader()

    def execute(operation, args):
        assert operation == "channel.add_sampler" and args == (str(source), 2)
        state["count"] += 1
        state["writes"] += 1
        return 2

    monkeypatch.setattr(loader, "execute", execute)
    request = {"context": module.effect_context(), "path": str(source), "expected_count": 2}
    preview = module.dispatch("native.addSampler", {**request, "dry_run": True})
    assert preview["success"] and not preview["submitted"] and state["writes"] == 0
    result = module.dispatch("native.addSampler", request)
    assert not result["success"] and result["submitted"] and result["state"] == "partial"
    assert result["index"] == 2 and not result["source_path_verified"]
    stale = module.dispatch("native.addSampler", request)
    assert not stale["success"] and not stale["submitted"] and state["writes"] == 1


def test_sampler_tool_preflights_wav_and_reports_partial(monkeypatch, tmp_path):
    import wave
    from fl_studio_mcp.tools import semantic_native as tools

    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\0\0" * 16)

    class Connection:
        def send_command(self, action, params, timeout=10):
            if action == "native.call" and params["method"] == "channelCount":
                return {"success": True, "result": 3 if self.submitted else 2}
            if action == "native.call" and params["method"] == "getChannelType":
                return {"success": True, "result": 0}
            if action == "native.call" and params["method"] == "getChannelName":
                return {"success": True, "result": "test"}
            assert action == "native.addSampler" and params["expected_count"] == 2
            self.submitted = True
            return {"success": False, "submitted": True, "state": "partial", "index": 2,
                    "context": "session:0", "channel_name": "test",
                    "native_request_id": params["request_id"], "source_path_verified": False}

    conn = Connection()
    conn.submitted = False
    monkeypatch.setattr(tools, "get_connection", lambda: conn)
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "session:0",
        "native": {"available": True, "operations": ["channel.add_sampler"]},
    })
    result = tools.fl_add_sampler(str(path))
    assert result["state"] == "partial" and result["channel_verified"]
    assert not result["success"] and not result["source_path_verified"]
    assert not tools.fl_add_sampler("relative.wav")["submitted"]


def test_sampler_readback_exception_preserves_submitted_status(semantic, monkeypatch, tmp_path):
    module = semantic.module
    source = tmp_path / "sample.wav"
    source.write_bytes(b"fixture")
    channels = sys.modules["channels"]
    monkeypatch.setattr(channels, "channelCount", lambda *_: 2)

    def fail_after_submission(*_):
        raise RuntimeError("readback unavailable")

    monkeypatch.setattr(channels, "getChannelType", fail_after_submission, raising=False)
    monkeypatch.setattr(module.native_effect_loader(), "execute", lambda *_: 2)
    result = module.dispatch("native.addSampler", {
        "context": module.effect_context(), "path": str(source), "expected_count": 2,
    })
    assert not result["success"] and result["submitted"] and result["state"] == "unconfirmed"


def test_generator_verified_when_plugin_name_matches(semantic, monkeypatch, tmp_path):
    module = semantic.module
    source = tmp_path / "3x Osc.fst"
    source.write_bytes(b"fixture")
    state = {"count": 2, "writes": 0}
    channels = sys.modules["channels"]
    monkeypatch.setattr(channels, "channelCount", lambda *_: state["count"])
    monkeypatch.setattr(channels, "getChannelType", lambda *_: 4, raising=False)
    monkeypatch.setattr(channels, "getChannelName", lambda *_: "3x Osc", raising=False)
    loader = module.native_effect_loader()

    def execute(operation, args):
        assert operation == "channel.add_generator"
        assert args == (str(source), 2)
        # The native op reads +0x160 immediately (still the default 0 on this
        # build); acceptance must gate on the later public readback and name.
        state["count"] += 1
        state["writes"] += 1
        return 2, 0, "3x Osc"

    monkeypatch.setattr(loader, "execute", execute)
    request = {"context": module.effect_context(), "path": str(source),
               "expected_count": 2, "plugin_name": "3x Osc"}
    preview = module.dispatch("native.addGenerator", {**request, "dry_run": True})
    assert preview["success"] and not preview["submitted"] and state["writes"] == 0
    result = module.dispatch("native.addGenerator", request)
    assert result["success"] and result["submitted"] and result["state"] == "verified"
    assert result["source_path_verified"] and result["index"] == 2
    assert result["plugin_name"] == "3x Osc" and result["channel_type"] == 4
    stale = module.dispatch("native.addGenerator", request)
    assert not stale["success"] and not stale["submitted"] and state["writes"] == 1


def test_generator_rejects_plugin_name_mismatch(semantic, monkeypatch, tmp_path):
    module = semantic.module
    source = tmp_path / "3x Osc.fst"
    source.write_bytes(b"fixture")
    channels = sys.modules["channels"]
    state = {"count": 2}
    monkeypatch.setattr(channels, "channelCount", lambda *_: state["count"])
    monkeypatch.setattr(channels, "getChannelType", lambda *_: 4, raising=False)
    monkeypatch.setattr(channels, "getChannelName", lambda *_: "something else", raising=False)

    def execute(*_):
        state["count"] += 1
        return 2, 4, "FLEX"

    monkeypatch.setattr(module.native_effect_loader(), "execute", execute)
    result = module.dispatch("native.addGenerator", {
        "context": module.effect_context(), "path": str(source),
        "expected_count": 2, "plugin_name": "3x Osc",
    })
    assert not result["success"] and result["submitted"] and result["state"] == "unconfirmed"
    assert result["observed_plugin"] == "FLEX"


def test_generator_tool_validates_fst_and_reports_verified(monkeypatch, tmp_path):
    from fl_studio_mcp.tools import semantic_native as tools

    # Build a minimal but structurally valid FLhd/FLdt FST under a Generators DB folder.
    db = tmp_path / "Plugin database" / "Generators"
    db.mkdir(parents=True)
    preset = db / "3x Osc.fst"
    payload = b"FLdt"
    header = b"FLhd\x06\0\0\0\x30\0" + b"\0" * 4 + payload + len(payload).to_bytes(4, "little")
    preset.write_bytes(header)

    class Connection:
        def send_command(self, action, params, timeout=10):
            if action == "native.call" and params["method"] == "channelCount":
                return {"success": True, "result": 3 if self.submitted else 2}
            if action == "native.call" and params["method"] == "getChannelType":
                return {"success": True, "result": 4}
            assert action == "native.addGenerator" and params["expected_count"] == 2
            self.submitted = True
            return {"success": True, "submitted": True, "state": "verified", "index": 2,
                    "context": "session:0", "plugin_name": "3x Osc",
                    "source_path_verified": True,
                    "native_request_id": params["request_id"]}

    conn = Connection()
    conn.submitted = False
    monkeypatch.setattr(tools, "get_connection", lambda: conn)
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "session:0",
        "native": {"available": True, "operations": ["channel.add_generator"],
                   "engine_path": str(tmp_path / "FLEngine_x64.dll")},
    })
    result = tools.fl_add_generator("3x Osc")
    assert result["success"] and result["state"] == "verified"
    assert result["channel_verified"] and result["source_path_verified"]
    assert not tools.fl_add_generator("")["submitted"]


def test_generator_tool_rejects_wrong_database_folder(monkeypatch, tmp_path):
    from fl_studio_mcp.tools import semantic_native as tools

    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "session:0",
        "native": {"available": True, "operations": ["channel.add_generator"],
                   "engine_path": str(tmp_path / "FLEngine_x64.dll")},
    })
    preset = tmp_path / "Plugin database" / "Effects" / "wrong.fst"
    preset.parent.mkdir(parents=True)
    preset.write_bytes(b"FLhd\x06\0\0\0\x30\0" + b"\0" * 18)
    result = tools.fl_add_generator("wrong", preset_path=str(preset))
    assert not result["success"] and not result["submitted"]


def test_installer_upgrades_existing_controller_refresh_hook():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "semantic_installer_test", root / "scripts/install_semantic_native.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    current = (root / "fl_controller/device_FLStudioMCP.py").read_text(encoding="utf-8")
    previous = current.replace(
        "    if semantic_native is not None:\n"
        "        semantic_native.playlist_refreshed()\n", "", 1,
    )
    assert previous != current
    assert installer.integrate_controller(previous) == current
    assert installer.integrate_controller(current) == current


def test_move_preview_and_ambiguous_clip_rejection(semantic):
    module = semantic.module
    context = module.effect_context()
    page = module.dispatch("native.listPlaylistClips", {"context": context})
    snapshot = page["clips"][0]["snapshot_ref"]
    original = bytes(semantic.rows[0])
    request = {"context": context, "snapshot_ref": snapshot, "start_tick": 1}
    preview = module.dispatch("native.movePlaylistClip", {**request, "dry_run": True})
    assert preview["success"] and preview["dry_run"] and not preview["submitted"]
    assert preview["before"]["start_tick"] == 0 and preview["after"]["start_tick"] == 1
    assert bytes(semantic.rows[0]) == original and not semantic.undo
    semantic.rows[1][:] = original
    ambiguous = module.dispatch("native.movePlaylistClip", request)
    assert not ambiguous["success"] and "Ambiguous" in ambiguous["error"]
    assert bytes(semantic.rows[0]) == original and not semantic.undo


def test_move_verification_checks_complete_snapshot(monkeypatch):
    from fl_studio_mcp.tools import semantic_native as tools

    record = bytearray(36)
    struct.pack_into("<iIih", record, 0, 0, 0x50090000, 384, 499)
    context = "session:0"
    snapshot = f"{context}:p0:0:0:{record.hex()}"

    class Connection:
        def send_command(self, action, params, timeout):
            assert action == "native.movePlaylistClip"
            return {"success": True, "submitted": True,
                    "native_request_id": params["request_id"]}

    monkeypatch.setattr(tools, "get_connection", lambda: Connection())
    observed_record = bytearray(record)
    struct.pack_into("<i", observed_record, 0, 1)
    clip = {"index": 0, "start_tick": 1, "track": 1,
            "snapshot_ref": f"{context}:p1:0:0:{observed_record.hex()}"}
    monkeypatch.setattr(tools, "fl_list_playlist_clips", lambda **kwargs: {
        "success": True, "context": context, "arrangement": 0, "clips": [clip],
    })
    assert tools.fl_move_playlist_clip(snapshot, 1)["verified"]
    observed_record[20] ^= 1  # A field beyond the basic source/length/position fields changed.
    clip["snapshot_ref"] = f"{context}:p1:0:0:{observed_record.hex()}"
    result = tools.fl_move_playlist_clip(snapshot, 1)
    assert result["submitted"] and not result["verified"] and not result["success"]


def test_automation_paging_and_channel_type_rejection(semantic):
    module = semantic.module
    context = module.effect_context()
    result = module.dispatch("native.readAutomationPoints", {
        "context": context, "channel": 5, "offset": 1, "limit": 1,
    })
    assert result["success"] and result["points"][0]["time_beats"] == 2.0
    assert result["points"][0]["value"] == 0.75 and result["next_offset"] is None
    rejected = module.dispatch("native.readAutomationPoints", {"context": context, "channel": 6})
    assert not rejected["success"] and "Automation Clip" in rejected["error"]


def test_automation_links_paging_names_and_channel_rejection(semantic):
    module = semantic.module
    context = module.effect_context()
    result = module.dispatch("native.readAutomationLinks", {
        "context": context, "channel": 5, "offset": 1, "limit": 1, "include_names": True,
        "include_values": True,
    })
    assert result["success"] and result["count"] == 2 and result["next_offset"] is None
    assert result["links"] == [{"index": 1, "target_event_id": 1904246790,
                                "target_name": "event 1904246790", "target_value": None}]
    first = module.dispatch("native.readAutomationLinks", {
        "context": context, "channel": 5, "limit": 1, "include_values": True,
    })
    assert first["links"][0]["target_value"] == 0.625
    rejected = module.dispatch("native.readAutomationLinks", {"context": context, "channel": 6})
    assert not rejected["success"] and "Automation Clip" in rejected["error"]


def test_automation_target_values_reject_old_controller(monkeypatch):
    from fl_studio_mcp.tools import semantic_native as tools

    class Connection:
        def send_command(self, action, params, timeout):
            assert action == "native.readAutomationLinks" and params["include_values"]
            return {"success": True, "native_request_id": params["request_id"],
                    "links": [{"target_event_id": 123}]}

    monkeypatch.setattr(tools, "get_connection", lambda: Connection())
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "session:0",
        "native": {"available": True, "operations": ["automation.read_links"]},
    })
    result = tools.fl_list_automation_links(5, include_values=True)
    assert not result["success"] and "updated controller" in result["error"]


def test_resolve_linked_effect_parameter_checks_instance(semantic, monkeypatch):
    module = semantic.module
    loader = module.native_effect_loader()
    original_execute = loader.execute
    ids = [0x70008000 + ((3 * 64 + slot) << 16) + 1 for slot in (0, 1)]

    def execute(operation, args):
        if operation == "automation.read_links":
            return 5, 0x50000, 2, b"".join(struct.pack("<I", event) for event in ids)
        return original_execute(operation, args)

    monkeypatch.setattr(loader, "execute", execute)
    monkeypatch.setattr(module.plugins, "isValid", lambda track, slot, global_index: True,
                        raising=False)
    monkeypatch.setattr(module.plugins, "getParamCount", lambda *args: 2, raising=False)
    monkeypatch.setattr(module.plugins, "getPluginName", lambda *args: "Fruity Balance",
                        raising=False)
    monkeypatch.setattr(module.plugins, "getParamName", lambda *args: "Volume", raising=False)
    result = module.dispatch("native.readAutomationLinks", {
        "context": module.effect_context(), "channel": 5, "limit": 16,
        "include_targets": True,
    })
    assert result["success"]
    assert result["count"] == 2 and result["next_offset"] is None
    assert [link["target"]["slot"] for link in result["links"]] == [0, 1]
    assert all(link["target"]["track"] == 3 and link["target"]["parameter"] == 1
               for link in result["links"])
    monkeypatch.setattr(module.plugins, "isValid", lambda *args: False)
    result = module.dispatch("native.readAutomationLinks", {
        "context": module.effect_context(), "channel": 5, "limit": 16,
        "include_targets": True,
    })
    assert all(link["target"] is None for link in result["links"])


def test_link_target_parameter_count_does_not_change_pagination(semantic, monkeypatch):
    module = semantic.module
    loader = module.native_effect_loader()
    event = 0x70008000 + ((3 * 64) << 16) + 1
    monkeypatch.setattr(loader, "execute", lambda *_: (5, 0x50000, 1, struct.pack("<I", event)))
    monkeypatch.setattr(module.plugins, "isValid", lambda *args: True, raising=False)
    monkeypatch.setattr(module.plugins, "getParamCount", lambda *args: 2, raising=False)
    monkeypatch.setattr(module.plugins, "getPluginName", lambda *args: "Effect", raising=False)
    monkeypatch.setattr(module.plugins, "getParamName", lambda *args: "Volume", raising=False)
    result = module.dispatch("native.readAutomationLinks", {
        "context": module.effect_context(), "channel": 5, "limit": 16,
        "include_targets": True,
    })
    assert result["success"] and result["count"] == 1 and result["next_offset"] is None


def test_automation_value_preview_edit_and_stale_snapshot(semantic):
    module = semantic.module
    context = module.effect_context()
    page = module.dispatch("native.readAutomationPoints", {"context": context, "channel": 5})
    point = page["points"][0]
    other = bytes(semantic.points[1])
    request = {"context": context, "snapshot_ref": point["snapshot_ref"], "value": 0.8}
    preview = module.dispatch("native.setAutomationPointValue", {**request, "dry_run": True})
    assert preview["success"] and not preview["submitted"] and not semantic.undo
    result = module.dispatch("native.setAutomationPointValue", request)
    assert result["success"] and result["submitted"] and len(semantic.undo) == 1
    assert struct.unpack_from("<f", semantic.points[0], 4)[0] == pytest.approx(0.8)
    assert bytes(semantic.points[1]) == other
    stale = module.dispatch("native.setAutomationPointValue", request)
    assert not stale["success"] and not stale["submitted"] and len(semantic.undo) == 1


def test_automation_snapshot_rejects_changed_preceding_time(semantic):
    module = semantic.module
    context = module.effect_context()
    page = module.dispatch("native.readAutomationPoints", {"context": context, "channel": 5})
    snapshot = page["points"][1]["snapshot_ref"]
    struct.pack_into("<f", semantic.points[0], 0, 0.25)
    result = module.dispatch("native.setAutomationPointValue", {
        "context": context, "snapshot_ref": snapshot, "value": 0.3,
    })
    assert not result["success"] and not result["submitted"] and not semantic.undo


def test_automation_tension_preview_edit_and_invalid(semantic):
    module = semantic.module
    context = module.effect_context()
    page = module.dispatch("native.readAutomationPoints", {"context": context, "channel": 5})
    point = page["points"][0]
    request = {"context": context, "snapshot_ref": point["snapshot_ref"], "tension": -0.75}
    preview = module.dispatch("native.setAutomationPointTension", {**request, "dry_run": True})
    assert preview["success"] and not preview["submitted"] and not semantic.undo
    invalid = module.dispatch("native.setAutomationPointTension", {**request, "tension": -1.1})
    assert not invalid["success"] and not semantic.undo
    edited = module.dispatch("native.setAutomationPointTension", request)
    assert edited["success"] and edited["submitted"] and len(semantic.undo) == 1
    assert struct.unpack_from("<f", semantic.points[0], 8)[0] == -0.75
    assert struct.unpack_from("<f", semantic.points[0], 4)[0] == 0.25
    stale = module.dispatch("native.setAutomationPointTension", request)
    assert not stale["success"] and not stale["submitted"] and len(semantic.undo) == 1


def test_automation_move_preserves_next_and_rejects_stale(semantic):
    module = semantic.module
    context = module.effect_context()
    before = module.dispatch("native.readAutomationPoints", {"context": context, "channel": 5})
    first, point = before["points"]
    bad_first = module.dispatch("native.moveAutomationPoint", {
        "context": context, "snapshot_ref": first["snapshot_ref"], "time_beats": 1.0,
    })
    assert not bad_first["success"] and not bad_first["submitted"]
    request = {"context": context, "snapshot_ref": point["snapshot_ref"], "time_beats": 1.0}
    preview = module.dispatch("native.moveAutomationPoint", {**request, "dry_run": True})
    assert preview["success"] and not preview["submitted"] and not semantic.undo
    moved = module.dispatch("native.moveAutomationPoint", request)
    assert moved["success"] and moved["submitted"]
    after = module.dispatch("native.readAutomationPoints", {"context": context, "channel": 5})
    assert [p["time_beats"] for p in after["points"]] == [0.0, 1.0]
    stale = module.dispatch("native.moveAutomationPoint", request)
    assert not stale["success"] and not stale["submitted"] and len(semantic.undo) == 1


def test_curve_snapshot_rejects_changed_context_between_pages(monkeypatch):
    from fl_studio_mcp.tools import semantic_native as tools

    def page(channel, offset, limit):
        assert channel == 5 and limit == 64
        return {"success": True, "context": "old" if offset == 0 else "new",
                "source_event": 123, "count": 65,
                "points": [{} for _ in range(64 if offset == 0 else 1)],
                "next_offset": 64 if offset == 0 else None}

    monkeypatch.setattr(tools, "fl_list_automation_points", page)
    result = tools._automation_curve_snapshot(5)
    assert not result["success"] and "changed" in result["error"]


def test_curve_edit_rejects_endpoints_without_submitting(monkeypatch):
    from fl_studio_mcp.tools import semantic_native as tools

    points = [{"snapshot_ref": "first", "time_beats": 0, "curve": 0},
              {"snapshot_ref": "last", "time_beats": 8, "curve": 0}]
    monkeypatch.setattr(tools, "_automation_curve_snapshot", lambda channel: {
        "success": True, "context": "c", "source": 1, "points": points,
    })
    monkeypatch.setattr(tools, "get_connection", lambda: pytest.fail("must not submit"))
    invalid = tools._edit_automation_points(5, "delete", 0, 0, 0, "first", False)
    assert not invalid["success"] and not invalid["submitted"]
    invalid = tools.fl_add_automation_point(5, 8, 0.5)
    assert not invalid["success"] and not invalid["submitted"]
    points[1]["curve"] = 1
    invalid = tools.fl_add_automation_point(5, 4, 0.5)
    assert not invalid["success"] and "linear" in invalid["error"]


def test_capability_operation_does_not_shadow_existing_describe(semantic):
    assert "native.describe" not in semantic.module.ACTIONS
    assert "native.semanticDescribe" in semantic.module.ACTIONS
    result = semantic.module.dispatch("native.semanticDescribe", {"request_id": "probe"})
    assert result["capability_request_id"] == "probe" and result["native"]["available"]


def _flp_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _minimal_flp_with_sampler(path: str) -> bytes:
    events = bytearray()
    # Channel 0: a generator (ChannelID.New=64, WORD iid=0), no sample path.
    events.append(64)
    events += (0).to_bytes(2, "little")
    # Channel 1: a Sampler with a SamplePath (196, TEXT+4).
    events.append(64)
    events += (1).to_bytes(2, "little")
    encoded = path.encode("utf-16-le") + b"\x00\x00"
    events.append(196)
    events += _flp_varint(len(encoded))
    events += encoded
    header = b"FLhd" + (6).to_bytes(4, "little") + (0).to_bytes(2, "little") \
        + (2).to_bytes(2, "little") + (96).to_bytes(2, "little")
    header += b"FLdt" + len(events).to_bytes(4, "little")
    return header + bytes(events)


def test_parse_flp_sampler_sources_maps_channel_index(monkeypatch):
    from fl_studio_mcp.tools import semantic_native as tools

    flp = _minimal_flp_with_sampler("C://samples//kick.wav")
    sources = tools.parse_flp_sampler_sources(flp)
    assert sources == [{"channel_index": 1, "channel_iid": 1,
                        "sample_path": "C://samples//kick.wav"}]

    # A header/size mismatch and a non-FLP payload are rejected.
    import pytest
    with pytest.raises(ValueError):
        tools.parse_flp_sampler_sources(b"not an flp")
    with pytest.raises(ValueError):
        tools.parse_flp_sampler_sources(flp[:-1])


def test_read_sampler_sources_uses_copy_and_reports_paths(monkeypatch, tmp_path):
    from fl_studio_mcp.tools import semantic_native as tools

    captured = {}
    def fake_save(path):
        captured["path"] = path
        Path(path).write_bytes(_minimal_flp_with_sampler("C://samples//snare.wav"))
        return {"success": True, "submitted": True, "context": "session:0"}
    monkeypatch.setattr(tools, "fl_save_project_copy", fake_save)

    result = tools.fl_read_sampler_sources()
    assert result["success"] and result["count"] == 1
    assert result["sources"][0]["sample_path"] == "C://samples//snare.wav"
    assert result["context"] == "session:0"
    # The temporary copy is removed after reading.
    assert not Path(captured["path"]).exists()


def test_read_sampler_sources_reports_save_failure(monkeypatch):
    from fl_studio_mcp.tools import semantic_native as tools

    monkeypatch.setattr(tools, "fl_save_project_copy",
                        lambda path: {"success": False, "error": "save-copy failed"})
    result = tools.fl_read_sampler_sources()
    assert not result["success"] and "save-copy failed" in result["error"]


def test_replace_sample_semantic_verifies_identity_and_rejects_non_sampler(
    semantic, monkeypatch, tmp_path,
):
    module = semantic.module
    source = tmp_path / "new.wav"
    source.write_bytes(b"fixture")
    channels = sys.modules["channels"]
    monkeypatch.setattr(channels, "channelCount", lambda *_: 3)
    monkeypatch.setattr(channels, "getChannelType", lambda *_: 0, raising=False)
    monkeypatch.setattr(channels, "getChannelName", lambda *_: "sampler", raising=False)
    loader = module.native_effect_loader()

    def execute(operation, args):
        assert operation == "channel.replace_sample"
        assert args == (1, str(source), 3)
        return 1

    monkeypatch.setattr(loader, "execute", execute)
    base = {"context": module.effect_context(), "channel": 1,
            "expected_count": 3, "path": str(source)}
    dry = module.dispatch("native.replaceSample", {**base, "dry_run": True})
    assert dry["success"] and not dry["submitted"] and dry["dry_run"]
    result = module.dispatch("native.replaceSample", base)
    assert result["success"] and result["submitted"] and result["state"] == "applied"
    assert result["index"] == 1 and result["channel_name"] == "sampler"

    # A non-Sampler target is rejected before submission.
    monkeypatch.setattr(channels, "getChannelType", lambda *_: 4, raising=False)
    bad = module.dispatch("native.replaceSample", base)
    assert not bad["success"] and not bad["submitted"]


def test_replace_sample_tool_readback_and_neighbor_guard(monkeypatch, tmp_path):
    import wave

    from fl_studio_mcp.tools import semantic_native as tools

    path = tmp_path / "replacement.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\0\0" * 16)

    before_sources = [
        {"channel_index": 1, "channel_iid": 1, "sample_path": "C://old//a.wav"},
        {"channel_index": 2, "channel_iid": 2, "sample_path": "C://keep//b.wav"},
    ]
    after_sources = [
        {"channel_index": 1, "channel_iid": 1, "sample_path": str(path)},
        {"channel_index": 2, "channel_iid": 2, "sample_path": "C://keep//b.wav"},
    ]
    state = {"reads": 0}
    def fake_read():
        state["reads"] += 1
        sources = before_sources if state["reads"] == 1 else after_sources
        return {"success": True, "sources": sources}
    monkeypatch.setattr(tools, "fl_read_sampler_sources", fake_read)
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "session:0",
        "native": {"available": True, "operations": ["channel.replace_sample"]},
    })

    class Conn:
        def send_command(self, action, params, timeout=10):
            if action == "native.call" and params["method"] == "channelCount":
                return {"success": True, "result": 3}
            assert action == "native.replaceSample" and params["channel"] == 1
            return {"success": True, "submitted": True, "state": "applied",
                    "index": 1, "context": "session:0",
                    "native_request_id": params["request_id"]}
    monkeypatch.setattr(tools, "get_connection", lambda: Conn())

    result = tools.fl_replace_sampler_sample(1, str(path))
    assert result["success"] and result["source_verified"]
    assert result["other_sources_unchanged"] and result["previous_path"] == "C://old//a.wav"
    assert result["new_path"] == str(path)


def test_replace_sample_tool_rejects_same_path_and_bad_channel(monkeypatch, tmp_path):
    import wave

    from fl_studio_mcp.tools import semantic_native as tools

    path = tmp_path / "same.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\0\0" * 16)
    monkeypatch.setattr(tools, "fl_read_sampler_sources", lambda: {
        "success": True,
        "sources": [{"channel_index": 1, "channel_iid": 1, "sample_path": str(path)}],
    })
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "session:0",
        "native": {"available": True, "operations": ["channel.replace_sample"]},
    })
    assert not tools.fl_replace_sampler_sample(1, str(path))["submitted"]
    assert not tools.fl_replace_sampler_sample(-1, str(path))["submitted"]


def test_parse_flp_plugin_states_real_fx_slot():
    from fl_studio_mcp.tools.semantic_native import parse_flp_plugin_states

    # Use the saved real-project copy when present; otherwise skip cleanly.
    evidence = Path('D:/hmdp/artifacts/fl-studio-interface-audit-20261115/'
                    'exact-slot-live-by92hagg/effect-at-3-8.flp')
    if not evidence.exists():
        import pytest
        pytest.skip('real FL 26 effect-state FLP not present')
    result = parse_flp_plugin_states(evidence.read_bytes())
    assert result['framing'] == 'tagged'
    assert result['version'] == '26.1.7.5654'
    assert result['mixer_slots'].get((3, 7)), result['mixer_slots']
    assert len(result['mixer_slots'][(3, 7)]) > 16

    baseline = evidence.with_name('baseline.flp')
    if baseline.exists():
        assert parse_flp_plugin_states(baseline.read_bytes())['mixer_slots'] == {}


def test_read_mixer_effect_state_lists_slots_and_rejects_bad_args(monkeypatch, tmp_path):
    from fl_studio_mcp.tools import semantic_native as tools

    state_bytes = b'\x01\x02\x03' * 8
    def fake_save(path):
        Path(path).write_bytes(
            b'FLhd' + (6).to_bytes(4, 'little') + (0).to_bytes(2, 'little')
            + (0).to_bytes(2, 'little') + (96).to_bytes(2, 'little')
            + b'FLdt' + (0).to_bytes(4, 'little'))
        return {"success": True, "context": "session:0"}
    monkeypatch.setattr(tools, "fl_save_project_copy", fake_save)
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "session:0",
        "native": {"available": True, "operations": ["project.save_copy"]},
    })
    monkeypatch.setattr(tools, "parse_flp_plugin_states", lambda _: {
        "framing": "tagged", "version": "26.1.7.5654", "channels": {},
        "mixer_slots": {(3, 7): state_bytes}})

    listed = tools.fl_read_mixer_effect_state()
    assert listed["success"] and listed["count"] == 1
    assert listed["mixer_slots"][0]["sha256"]

    one = tools.fl_read_mixer_effect_state(3, 7)
    assert one["success"] and one["bytes"] == len(state_bytes)
    assert not tools.fl_read_mixer_effect_state(3)["success"]
    assert not tools.fl_read_mixer_effect_state(3, 99)["success"]


def _automation_point(index, time_beats, value, channel=2, source=65536):
    return {"index": index, "time_beats": time_beats, "value": value, "tension": 0.0,
            "curve": 0,
            "snapshot_ref": ("s:2:%d:%d:0x0.0p+0:%s" % (source, index, "00" * 32))}


def test_shape_automation_segment_dry_run_plans_ease_in(monkeypatch):
    from fl_studio_mcp.tools import semantic_native as tools

    points = [_automation_point(0, 0.0, 0.2), _automation_point(1, 4.0, 0.8)]
    monkeypatch.setattr(tools, "fl_list_automation_points",
                        lambda *a, **k: {"success": True, "points": points})
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "s:2:0",
        "native": {"available": True, "operations": ["native.editAutomationPoints"]},
    })

    dry = tools.fl_shape_automation_segment(2, 0, "ease_in", 8, dry_run=True)
    assert dry["success"] and not dry["submitted"] and dry["dry_run"]
    assert len(dry["planned"]) == 7
    assert abs(dry["planned"][0]["value"] - (0.2 + 0.6 * (1 / 8) ** 2)) < 1e-9
    assert dry["planned"][3]["time_beats"] == 2.0

    # ease_out is the mirror of ease_in.
    out = tools.fl_shape_automation_segment(2, 0, "ease_out", 8, dry_run=True)
    assert abs(out["planned"][0]["value"] - (0.2 + 0.6 * (1 - (1 - 1 / 8) ** 2))) < 1e-9


def test_shape_automation_segment_inserts_and_reports_undo_steps(monkeypatch):
    from fl_studio_mcp.tools import semantic_native as tools

    calls = []
    class Conn:
        def send_command(self, action, params, timeout=10):
            assert action == "native.editAutomationPoints"
            calls.append(params)
            return {"success": True, "native_request_id": params["request_id"]}
    monkeypatch.setattr(tools, "get_connection", lambda: Conn())
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "s:2:0",
        "native": {"available": True, "operations": ["native.editAutomationPoints"]},
    })
    monkeypatch.setattr(tools, "fl_list_automation_points", lambda *a, **k: {
        "success": True,
        "points": [_automation_point(0, 0.0, 0.2), _automation_point(1, 4.0, 0.8)],
    })
    result = tools.fl_shape_automation_segment(2, 0, "ease_in_out", 8)
    assert result["success"] and result["inserted"] == 7
    assert result["undo_steps"] == 7
    assert len(calls) == 7
    assert calls[0]["index"] == 1 and calls[-1]["index"] == 7
    assert calls[0]["mode"] == "insert" and calls[0]["tension"] == 0.0


def test_fst_with_plugin_state_replaces_213_and_fixes_length():
    from fl_studio_mcp.tools.semantic_native import _flp_event_layout, _fst_with_plugin_state

    # Minimal FST: FLhd(6-byte header) + FLdt with three events.
    def event(eid, payload):
        # Ids below 192 use fixed payload sizes with no length prefix.
        if eid < 64:
            assert len(payload) == 1
            return bytes([eid]) + payload
        if eid < 128:
            assert len(payload) == 2
            return bytes([eid]) + payload
        if eid < 192:
            assert len(payload) == 4
            return bytes([eid]) + payload
        assert len(payload) < 128
        return bytes([eid, len(payload)]) + payload

    body = event(199, b"12.5.0.45\0") + event(128, b"HQV\0") + event(213, b"\x00\x00\x00\x00")
    header = b"FLhd" + (6).to_bytes(4, "little") + b"\x30\x00\x02\x00\x60\x00"
    skeleton = header + b"FLdt" + len(body).to_bytes(4, "little") + body

    chunk, head, events = _flp_event_layout(skeleton)
    assert [e["id"] for e in events] == [199, 128, 213]
    assert len(events[2]["payload"]) == 4

    state = b"\xdf\xff\xff\xff\x00\x01\x00\x00"
    out = _fst_with_plugin_state(skeleton, state)
    chunk2, head2, events2 = _flp_event_layout(out)
    assert [e["id"] for e in events2] == [199, 128, 213]
    assert events2[2]["payload"] == state
    # FLdt length must describe the rebuilt body exactly.
    assert int.from_bytes(out[chunk2 + 4:chunk2 + 8], "little") == len(out) - (chunk2 + 8)
    # Header fields (including the 0x30 effect marker) are preserved; the FLdt
    # length word legitimately changes with the new payload size.
    assert out[:18] == skeleton[:18]


def test_fst_with_plugin_state_appends_when_skeleton_has_no_213():
    from fl_studio_mcp.tools.semantic_native import _fst_with_plugin_state

    def event(eid, payload):
        # Ids below 192 use fixed payload sizes with no length prefix.
        if eid < 64:
            assert len(payload) == 1
            return bytes([eid]) + payload
        if eid < 128:
            assert len(payload) == 2
            return bytes([eid]) + payload
        if eid < 192:
            assert len(payload) == 4
            return bytes([eid]) + payload
        assert len(payload) < 128
        return bytes([eid, len(payload)]) + payload

    body = event(199, b"v\0")
    header = b"FLhd" + (6).to_bytes(4, "little") + b"\x30\x00\x01\x00\x60\x00"
    skeleton = header + b"FLdt" + len(body).to_bytes(4, "little") + body
    out = _fst_with_plugin_state(skeleton, b"\x01\x02\x03")
    from fl_studio_mcp.tools.semantic_native import _flp_event_layout
    _, _, events = _flp_event_layout(out)
    assert [e["id"] for e in events] == [199, 213]
    assert events[1]["payload"] == b"\x01\x02\x03"


def test_list_plugin_presets_scans_factory_folder(monkeypatch, tmp_path):
    from fl_studio_mcp.tools import semantic_native as tools

    engine = tmp_path / "FL64.exe"
    engine.touch()
    folder = tmp_path / "Data/Patches/Plugin presets/Effects/TestFX"
    (folder / "Sub").mkdir(parents=True)
    (folder / "Alpha.fst").write_bytes(b"PLACEHOLDER")
    (folder / "Sub" / "Beta.fst").write_bytes(b"PLACEHOLDER")
    monkeypatch.setattr(tools, "fl_list_capabilities", lambda: {
        "success": True, "context": "s:1:0",
        "native": {"available": True, "engine_path": str(engine)},
    })
    result = tools.fl_list_plugin_presets("TestFX", "effect")
    assert result["success"]
    names = {p["relative"]: p["origin"] for p in result["presets"]}
    assert names.get("Alpha.fst") == "factory"
    assert names.get("Sub\\Beta.fst") == "factory" or names.get("Sub/Beta.fst") == "factory"
    assert result["count"] >= 2


def test_checked_effect_preset_accepts_plugin_presets_folder(tmp_path):
    from fl_studio_mcp.tools.semantic_native import _checked_effect_preset

    folder = tmp_path / "Plugin presets" / "Effects" / "TestFX"
    folder.mkdir(parents=True)
    body = b"\x00" * 4
    header = b"FLhd" + (6).to_bytes(4, "little") + b"\x30\x00\x02\x00\x60\x00"
    image = header + b"FLdt" + len(body).to_bytes(4, "little") + body
    preset = folder / "Alpha.fst"
    preset.write_bytes(image)
    assert _checked_effect_preset(preset) == preset.resolve()

    bad = tmp_path / "Elsewhere" / "Alpha.fst"
    bad.parent.mkdir()
    bad.write_bytes(image)
    try:
        _checked_effect_preset(bad)
        assert False, "paths outside a preset database must be rejected"
    except ValueError:
        pass


def test_classify_plugin_state_vst3_and_fl_native():
    from fl_studio_mcp.tools.semantic_native import classify_plugin_state

    # A VST3 wrapper's record embeds the plugin's file path.
    vst_state = b"\x08\x00\x00\x00VST3\x00C://Program Files\\Common Files\\VST3\\FabFilter Timeless 3.vst3"
    vst = classify_plugin_state("FabFilter Timeless 3", vst_state)
    assert vst["adapter"] == "vst3-wrapper"
    assert vst["capabilities"]["wrapper_state_file"] is True
    assert vst["has_format_marker"] is True

    # FL's own effect keeps a compact record with no path.
    fl_state = b"\xdf\xff\xff\xff\x00\x01\x00\x00"
    native = classify_plugin_state("Fruity Balance", fl_state)
    assert native["adapter"] == "fl-native-effect"
    assert native["capabilities"]["wrapper_state_file"] is False
    assert native["has_format_marker"] is False

    # A wrapper-hosted plugin is recognised by name even without a path.
    wrapped = classify_plugin_state("Fruity Wrapper", b"\x00\x01\x02")
    assert wrapped["adapter"] == "vst-wrapper"
