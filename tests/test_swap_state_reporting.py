"""Stage-1 acceptance: swap state reporting and recovery-material retention.

Covers every one of the four writes being rejected/failed/timed-out in turn,
plus a final verification save-copy failure. The state field must distinguish
not_written / swap_verified / original_restored / outcome_unknown /
partial_modification, and unverified projects must keep their recovery files.
"""

import hashlib
import sys
import types
from pathlib import Path

import pytest

from fl_studio_mcp.tools import semantic_native as tools

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fx_compound_writes import Harness, uuid  # noqa: E402


@pytest.fixture
def harness(monkeypatch, tmp_path):
    return Harness(monkeypatch, tmp_path)


def _inject_at(harness, action, occurrence, response):
    original = harness.send_command
    seen = {"n": 0}

    def guarded(act, params, timeout=10):
        if act == action:
            seen["n"] += 1
            if seen["n"] == occurrence:
                harness.calls.append((act, params))
                return response(params) if callable(response) else response
        return original(act, params, timeout)

    harness.monkeypatch.setattr(
        tools, "get_connection",
        lambda: types.SimpleNamespace(send_command=guarded))


def _fault(kind):
    def factory(params):
        base = {"success": False, "queue_request_id": uuid()}
        if kind == "rejected":
            return {**base, "submitted": False, "error": "refused"}
        if kind == "failed":
            return {**base, "submitted": True, "verified": False,
                    "error": "handler failed",
                    "effect_request_id": params.get("request_id"),
                    "native_request_id": params.get("request_id")}
        return {**base, "submitted": True, "error": "timed out",
                "error_type": "OutcomeUnknown", "state": "outcome_unknown"}
    return factory


def _writes(harness):
    return [a for a, _ in harness.calls
            if a in ("native.removeMixerEffect", "mixer.loadEffectAtSlot")]


def test_swap_happy_path_state_is_swap_verified(harness):
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is True
    assert result["state"] == "swap_verified"
    assert result["state_matched"] is True
    # Presets were staged once, before any write; load steps reused them.
    assert not result.get("retained_artifacts")
    assert len([s for s in result["steps"] if s["step"].startswith("load")]) == 2


@pytest.mark.parametrize("action,occurrence", [
    ("native.removeMixerEffect", 1),
    ("native.removeMixerEffect", 2),
    ("mixer.loadEffectAtSlot", 1),
    ("mixer.loadEffectAtSlot", 2),
])
@pytest.mark.parametrize("kind", ["rejected", "failed", "timeout"])
def test_swap_state_for_each_write_fault(harness, action, occurrence, kind):
    _inject_at(harness, action, occurrence, _fault(kind))
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is False
    assert result["state"] in ("not_written", "outcome_unknown",
                               "partial_modification", "original_restored")
    if kind == "timeout":
        assert result["state"] == "outcome_unknown", result
        # No compensating write after an ambiguous result.
        assert not any(s["step"].startswith("restore_") for s in result["steps"])
    if kind == "rejected" and action == "native.removeMixerEffect" and occurrence == 1:
        assert result["state"] == "not_written"
    if result["state"] == "original_restored":
        assert result["restored"] is True
        assert result["a_after_sha256"] == hashlib.sha256(harness.original[(3, 5)]).hexdigest()
        assert result["b_after_sha256"] == hashlib.sha256(harness.original[(3, 7)]).hexdigest()
    assert result["irreversible"] is True
    assert result["status_request_ids"]


def test_stage_failure_writes_nothing(harness, monkeypatch):
    """An invalid staged preset stops before the first destructive call."""
    monkeypatch.setattr(tools, "_fst_with_plugin_state",
                        lambda skeleton, state: b"NOT-A-VALID-FST")
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is False
    assert result["state"] == "not_written"
    assert not _writes(harness)
    assert result["retained_artifacts"]


def test_final_readback_failure_retains_recovery_material(harness, monkeypatch):
    """Fourth write committed but verification failed: keep everything."""
    original = harness.save_copy
    calls = {"n": 0}

    def fail_verification(path):
        calls["n"] += 1
        if calls["n"] == 2:  # capture succeeds, verify fails
            # Make the steps terminal so the old cleanup would have deleted.
            return {"success": False, "submitted": True, "error": "reader failed",
                    "context": harness.context, "queue_request_id": uuid()}
        return original(path)

    monkeypatch.setattr(tools, "fl_save_project_copy", fail_verification)
    # All four writes really happened.
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert len(_writes(harness)) == 4
    assert result["success"] is False
    assert result["state"] == "partial_modification"
    assert result["submitted"] is True
    assert result["retained_artifacts"]
    assert all(Path(p).exists() for p in result["retained_artifacts"])
    assert "unverified" in result["note"]
    # Previously installed requests can still resolve by ID.
    assert result["status_request_ids"]


def test_unknown_write_has_no_retained_artifacts_deleted_prematurely(harness):
    _inject_at(harness, "mixer.loadEffectAtSlot", 1, _fault("timeout"))
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["state"] == "outcome_unknown"
    assert result["retained_artifacts"]
    assert all(Path(p).exists() for p in result["retained_artifacts"])
