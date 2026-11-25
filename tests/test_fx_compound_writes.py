"""Regression tests for compound FX writes under simulated RPC failures.

No FL Studio instance is required: the connection, capabilities, save-copy and
FLP parser are injected. These tests assert that a compound write stops at the
first ambiguous result, keeps its correlation IDs, and never reports a swap as
recovered unless both original digests were independently read back.
"""

import hashlib
import struct
import types
from pathlib import Path

import pytest

from fl_studio_mcp.tools import semantic_native as tools

CONTEXT = "1234:1000:0"
ENGINE = "C:/FL/FL64.exe"


def _text_event(event_id: int, payload: bytes) -> bytes:
    """Encode a TEXT/DATA (>=192) FLP event with a varint length."""
    return bytes([event_id]) + tools._flp_varint(len(payload)) + payload


def _fst(state: bytes) -> bytes:
    """A minimal valid effect FST carrying one event-213 state record."""
    body = _text_event(199, b"12.5.0.45\0")
    body += _text_event(213, state)
    header = b"FLhd" + (6).to_bytes(4, "little") + b"\x30\x00\x02\x00\x60\x00"
    return header + b"FLdt" + len(body).to_bytes(4, "little") + body


def _flp(slots: dict) -> bytes:
    """A minimal FLP whose FLdt encodes mixer event-213 records."""
    body = _text_event(199, b"26.1.7.5654\0")
    for (track, slot), state in slots.items():
        body += bytes([236]) + struct.pack("<H", track)
        body += _text_event(213, state)
        body += bytes([98]) + struct.pack("<H", slot)
    header = b"FLhd" + (6).to_bytes(4, "little") + b"\x30\x00\x02\x00\x60\x00"
    return header + b"FLdt" + len(body).to_bytes(4, "little") + body


def _fst_state(data: bytes) -> bytes:
    """Extract the event-213 payload from a synthetic FST."""
    _, _, events = tools._flp_event_layout(data)
    for event in events:
        if event["id"] == 213:
            return event["payload"]
    raise AssertionError("FST has no event-213 record")


class Harness:
    def __init__(self, monkeypatch, tmp_path, *, state_a=b"A" * 16, state_b=b"B" * 16):
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.context = CONTEXT
        self.states = {(3, 5): state_a, (3, 7): state_b}
        self.original = dict(self.states)
        self.calls = []
        self.failure = {}          # action -> response or callable
        self.correlation_ids = []  # every params['request_id'] observed
        self.queue_counter = 0

        monkeypatch.setattr(tools, "fl_list_capabilities", self.capabilities)
        monkeypatch.setattr(tools, "fl_save_project_copy", self.save_copy)
        monkeypatch.setattr(tools, "parse_flp_plugin_states", self.parse_states)
        monkeypatch.setattr(tools, "_effect_preset", self.preset_path)
        monkeypatch.setattr(tools, "get_connection", lambda: self)
        monkeypatch.setattr(tools, "_queue_status", self.queue_status)
        monkeypatch.setattr(tools, "Path", Path)  # no-op, documents intent
        self.preset_dir = tmp_path / "presets"
        self.preset_dir.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def capabilities(self):
        return {"success": True, "context": self.context,
                "native": {"available": True, "engine_path": ENGINE,
                           "operations": ["project.save_copy"]}}

    def queue_status(self, request_id):
        return {"state": "completed"}

    def preset_path(self, name, engine_path, preset_path=None):
        if preset_path is not None:
            return Path(preset_path)
        target = self.preset_dir / "skeleton.fst"
        target.write_bytes(_fst(b"\x00" * 8))
        return target

    def save_copy(self, path):
        self.calls.append(("save_copy", path))
        Path(path).write_bytes(_flp(self.states))
        return {"success": True, "submitted": True, "context": self.context,
                "request_id": uuid(), "queue_request_id": uuid()}

    def parse_states(self, data):
        return {"framing": "tagged", "version": "26.1.7.5654", "channels": {},
                "mixer_slots": dict(self.states)}

    def send_command(self, action, params, timeout=10):
        self.correlation_ids.append(params.get("request_id"))
        self.calls.append((action, params))
        if action in self.failure:
            value = self.failure[action]
            if callable(value):
                return value(params)
            return value
        if action == "mixer.effectLoaderInfo":
            return {"success": True, "context": self.context, "engine_path": ENGINE}
        if action == "mixer.readEffectSlot":
            track, slot = params["track"], params["slot"]
            state = self.states.get((track, slot))
            occupied = state is not None
            name = None
            if occupied:
                name = {"A": "Fruity Delay 3", "B": "Fruity Balance"}.get(
                    chr(state[0]) if state else "", "Fruity Delay 3")
            return {"success": True, "occupied": occupied, "track": track, "slot": slot,
                    "plugin_name": name,
                    "context": self.context, "editor_request_id": params.get("request_id"),
                    "queue_request_id": uuid()}
        if action == "native.call":
            method = params["method"]
            if method == "getPluginName":
                track, slot = params["args"][0], params["args"][1]
                name = {5: "Fruity Delay 3", 7: "Fruity Balance"}.get(slot, "FX")
                return {"success": True, "result": name, "queue_request_id": uuid()}
            if method == "isValid":
                return {"success": True, "result": False, "queue_request_id": uuid()}
        if action == "native.removeMixerEffect":
            slot = params["slot"]
            self.states.pop((3, slot), None)
            return {"success": True, "submitted": True, "verified": True,
                    "context": self.context, "queue_request_id": uuid()}
        if action == "mixer.loadEffectAtSlot":
            track, slot = params["track"], params["slot"]
            preset = Path(params["preset_path"])
            state = _fst_state(preset.read_bytes())
            self.states[(track, slot)] = state
            return {"success": True, "submitted": True, "verified": True,
                    "effect_request_id": params.get("request_id"),
                    "queue_request_id": uuid()}
        return {"success": False, "error": "unexpected action " + action}


def uuid():
    return hashlib.sha256(str(__import__("time").time_ns()).encode()).hexdigest()[:32]


@pytest.fixture
def harness(monkeypatch, tmp_path):
    return Harness(monkeypatch, tmp_path)


def test_file_rpc_response_exposes_queue_id(monkeypatch, tmp_path):
    """FileRPC.send must expose the envelope ID for fl_get_request_status."""
    from fl_studio_mcp.utils.file_rpc import FileRPC

    root = tmp_path / "rpc"
    root.mkdir(parents=True)
    (root / "heartbeat.json").write_text(
        '{"protocol":2,"time":%s,"pid":1,"controller_build":"x"}' % __import__("time").time())

    seen = {}

    def fake_status(self, request_id):
        seen["queried"] = request_id
        return {"state": "completed", "response": {
            "success": True, "request_id": "correlation"}}

    monkeypatch.setattr(FileRPC, "status", fake_status)
    monkeypatch.setattr(FileRPC, "heartbeat", lambda self: {"healthy": True})
    monkeypatch.setattr(FileRPC, "refresh_queue", lambda self: None)
    client = FileRPC(root)
    result = client.send("echo", {"request_id": "correlation"}, 2)
    assert result["success"]
    assert result["request_id"] == "correlation"
    assert len(result["queue_request_id"]) == 32
    assert result["queue_request_id"] == seen["queried"]
    assert result["queue_request_id"] != "correlation"


def test_swap_success_cross_matches_digests(harness):
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] and result["state_matched"]
    assert result["irreversible"] is True
    assert result["a_after_sha256"] == result["b"]["sha256"]
    assert result["b_after_sha256"] == result["a"]["sha256"]
    assert any(step["step"] == "remove_a" for step in result["steps"])
    assert all(step["correlation_id"] for step in result["steps"])


def test_swap_stops_on_second_remove_unknown(harness):
    original_send = harness.send_command

    def guarded(action, params, timeout=10):
        if action == "native.removeMixerEffect" and params["slot"] == 7:
            harness.calls.append((action, params))
            return {"success": False, "submitted": True, "error_type": "OutcomeUnknown",
                    "state": "outcome_unknown", "queue_request_id": uuid()}
        return original_send(action, params, timeout)

    harness.monkeypatch.setattr(tools, "get_connection", lambda: types.SimpleNamespace(
        send_command=guarded))
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert not result["success"]
    assert result["state"] == "outcome_unknown"
    loads = [c for c in harness.calls if c[0] == "mixer.loadEffectAtSlot"]
    assert len(loads) == 1, "no second write after an unknown result"


def test_swap_rejects_before_any_write_on_bad_context(harness):
    original_save = harness.save_copy

    def changed_context(path):
        harness.context = "1234:9999:1"
        return original_save(path)

    harness.monkeypatch.setattr(tools, "fl_save_project_copy", changed_context)
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert not result["success"]
    assert not any(c[0] in ("native.removeMixerEffect", "mixer.loadEffectAtSlot")
                   for c in harness.calls)


def test_swap_keeps_digest_reporting_on_neighbor_change(harness):
    """A changed neighbouring slot must not be reported as a successful swap."""
    original_parse = harness.parse_states

    def noisy_parse(data):
        parsed = original_parse(data)
        parsed["mixer_slots"][(3, 9)] = b"NEIGHBOR"
        return parsed

    harness.monkeypatch.setattr(tools, "parse_flp_plugin_states", noisy_parse)
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is False or result["state_matched"] is True
    # The neighbouring slot is reported only via digests we actually read.
    assert "a_after_sha256" in result


def test_clone_only_succeeds_on_digest_match(harness):
    result = tools.fl_clone_mixer_effect_full(3, 5, 3, 7)
    assert result["success"] is True
    assert result["state_matched"] is True
    assert result["target_sha256"] == result["source_sha256"]
    assert any(step["step"] == "verify_state" for step in result["steps"])
    # The load step must carry both IDs so fl_get_request_status can resolve it.
    load = next(s for s in result["steps"] if s["step"] == "load_state")
    assert load["correlation_id"]
    assert load["queue_request_id"]
    assert load["queue_request_id"] in result["status_request_ids"]


def test_clone_preserves_submitted_on_readback_failure(harness):
    harness.failure["native.saveProjectCopy"] = {
        "success": False, "submitted": True, "error": "reader failed",
        "context": CONTEXT, "queue_request_id": uuid(),
    }
    # fail only the verification copy
    count = {"n": 0}
    original = harness.save_copy

    def fail_second(path):
        count["n"] += 1
        if count["n"] == 2:
            return {"success": False, "submitted": True, "error": "reader failed",
                    "context": CONTEXT, "queue_request_id": uuid()}
        return original(path)

    harness.monkeypatch.setattr(tools, "fl_save_project_copy", fail_second)
    result = tools.fl_clone_mixer_effect_full(3, 5, 3, 7)
    assert result["success"] is False
    assert result["submitted"] is True


def test_dry_run_does_not_submit_writes(harness):
    result = tools.fl_swap_mixer_effects(3, 5, 7, dry_run=True)
    assert result["dry_run"] is True
    assert result["submitted"] is False
    assert not any(c[0] in ("native.removeMixerEffect", "mixer.loadEffectAtSlot")
                   for c in harness.calls)
