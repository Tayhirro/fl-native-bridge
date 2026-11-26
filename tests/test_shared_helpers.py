"""Stage-5 acceptance: semantic tools share the extracted helper modules.

The public tool names, signatures and response fields stay in
``tools.semantic_native``; pure FLP parsing and write-guard logic live in
shared modules and are re-exported so existing patch points keep working.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fl_studio_mcp.tools import semantic_native as tools  # noqa: E402
from fl_studio_mcp.utils import flp_events, write_guard  # noqa: E402


def test_flp_helpers_reexported():
    assert tools._flp_event_layout is flp_events.flp_event_layout
    assert tools._flp_varint is flp_events.flp_varint
    assert tools._fst_with_plugin_state is flp_events.fst_with_plugin_state


def test_write_guard_reexported():
    assert tools._step_outcome is not None
    assert tools._step_record is not None
    assert tools._pin is not None
    # The guard functions come from the shared module (wrappers inject the
    # patchable queue-status call).
    assert callable(write_guard.terminal)
    assert callable(write_guard.release_when_final)


def test_clone_and_swap_share_guard():
    """Both compound tools call the same _pin/_step_record wrappers."""
    import inspect

    clone_src = inspect.getsource(tools.fl_clone_mixer_effect_full)
    swap_src = inspect.getsource(tools.fl_swap_mixer_effects)
    for name in ("_pin(", "_step_record(", "_release_when_final("):
        assert name in clone_src, name
        assert name in swap_src, name
    # Both finish() paths route through the same guard module.
    assert "guard.release_when_final" in inspect.getsource(tools._release_when_final)


def test_step_classification_is_shared():
    assert write_guard.step_outcome({"success": False, "submitted": False}) == \
        "rejected_before_write"
    assert write_guard.step_outcome({"success": False, "submitted": True}) == \
        "known_failure"
    assert write_guard.step_outcome({"error_type": "OutcomeUnknown"}) == "outcome_unknown"
    assert write_guard.step_outcome({"success": True, "submitted": True,
                                     "verified": False}) == "partial"
    assert write_guard.step_outcome({"success": True}) == "completed"
