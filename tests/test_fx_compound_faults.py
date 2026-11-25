"""Failure-injection matrix for compound FX writes.

The harness is imported from test_fx_compound_writes so both suites share the
same simulated RPC fixture. Every removal and load step is faulted in turn;
the tests assert where writes stop, what can be resolved by the status tool,
and under which gates a compensating repair is permitted.
"""

import hashlib
import sys
import types
from pathlib import Path

import pytest

from fl_studio_mcp.tools import semantic_native as tools

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fx_compound_writes import Harness  # noqa: E402


def _inject_at(harness, action, occurrence, response):
    """Fail the Nth call of an action (1-based), leaving others intact."""
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


def _mutation_count(harness):
    return sum(1 for action, _ in harness.calls
               if action in ("native.removeMixerEffect", "mixer.loadEffectAtSlot"))


def _queue_id():
    return hashlib.sha256(str(__import__("time").time_ns()).encode()).hexdigest()[:32]


def _unknown(params):
    return {"success": False, "submitted": True, "error": "timed out",
            "error_type": "OutcomeUnknown", "state": "outcome_unknown",
            "queue_request_id": _queue_id()}


def _rejected(params):
    return {"success": False, "submitted": False, "error": "refused",
            "queue_request_id": _queue_id()}


def _failed(params):
    # A known handler failure must still correlate back to the tool request;
    # otherwise the wrapper correctly treats it as outcome_unknown.
    return {"success": False, "submitted": True, "error": "handler failed",
            "verified": False, "effect_request_id": params.get("request_id"),
            "queue_request_id": _queue_id()}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    return Harness(monkeypatch, tmp_path)


@pytest.mark.parametrize("action,occurrence,response,boundary", [
    ("native.removeMixerEffect", 1, _rejected, 0),
    ("native.removeMixerEffect", 1, _unknown, 1),
    ("native.removeMixerEffect", 1, _failed, 1),
    ("mixer.loadEffectAtSlot", 1, _unknown, 2),
    ("mixer.loadEffectAtSlot", 1, _failed, 2),
    ("native.removeMixerEffect", 2, _unknown, 3),
    ("native.removeMixerEffect", 2, _failed, 3),
    ("mixer.loadEffectAtSlot", 2, _unknown, 4),
    ("mixer.loadEffectAtSlot", 2, _failed, 4),
])
def test_swap_stops_at_each_fault(harness, action, occurrence, response, boundary):
    """Every removal/load fault is handled at the observed write boundary."""
    _inject_at(harness, action, occurrence, response)
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is False
    assert result["irreversible"] is True
    assert result["status_request_ids"]
    assert all(len(s["correlation_id"]) == 32 for s in result["steps"])
    # Repair writes are allowed only after a known failure; the count is
    # bounded by the failed boundary plus at most four compensation writes.
    assert _mutation_count(harness) <= boundary + 4


def test_swap_unknown_never_compensates(harness):
    """An ambiguous write must never trigger a compensating mutation."""
    _inject_at(harness, "mixer.loadEffectAtSlot", 1, _unknown)
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["state"] == "outcome_unknown"
    assert "restored" not in result
    assert "restore_reason" not in result
    assert _mutation_count(harness) == 2  # remove_a + the ambiguous load


def test_swap_known_failure_compensates_and_proves_both_digests(harness):
    """A known failure repairs both slots and verifies each original digest."""
    _inject_at(harness, "mixer.loadEffectAtSlot", 1, _failed)
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is False
    assert result["restore_reason"] == "load_b_into_a_failed"
    assert result["restored"] is True
    assert result["a_after_sha256"] == hashlib.sha256(harness.original[(3, 5)]).hexdigest()
    assert result["b_after_sha256"] == hashlib.sha256(harness.original[(3, 7)]).hexdigest()
    assert any(s["step"].startswith("restore_") for s in result["steps"])


def test_swap_compensation_blocked_while_request_inflight(harness):
    """No repair until every submitted request has left the queue."""
    _inject_at(harness, "mixer.loadEffectAtSlot", 1, _failed)
    harness.monkeypatch.setattr(tools, "_queue_status",
                               lambda request_id: {"state": "outcome_unknown"})
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["restored"] is False
    assert "not finished" in result["restore_blocked"]
    assert not any(s["step"].startswith("restore_") for s in result["steps"])


def test_swap_compensation_blocked_on_project_change(harness):
    """A project/controller switch during a failed swap forbids repair."""
    def switch_context(params):
        harness.context = "1234:9999:1"
        return _failed(params)

    _inject_at(harness, "mixer.loadEffectAtSlot", 1, switch_context)
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    # Either the loader refuses the stale context before writing (no repair
    # attempted), or compensation runs but is gated on the changed context.
    assert result["success"] is False
    if "restored" in result:
        assert result["restored"] is False
        assert "changed" in result["restore_blocked"]
    assert not any(s["step"].startswith("restore_") for s in result["steps"])


def test_swap_steps_include_slot_state(harness):
    """Mutation steps record the last observed slot identity."""
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is True
    for name in ("remove_a", "remove_b", "load_b_into_a", "load_a_into_b"):
        step = next(s for s in result["steps"] if s["step"] == name)
        assert step["slot_state"] in ({"track": 3, "slot": 5},
                                      {"track": 3, "slot": 7})


def test_swap_request_status_ids_are_queryable(harness):
    """Every queue ID reported can be resolved by the status tool."""
    result = tools.fl_swap_mixer_effects(3, 5, 7)
    assert result["success"] is True
    seen = set()
    for step in result["steps"]:
        qid = step.get("queue_request_id")
        if qid:
            assert qid in result["status_request_ids"]
            assert qid != step["correlation_id"]
            seen.add(qid)
    assert len(seen) >= 6
