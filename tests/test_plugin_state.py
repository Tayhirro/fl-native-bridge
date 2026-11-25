from copy import deepcopy

import pytest

from fl_studio_mcp.tools.plugin_state import PluginParameters


class RPC:
    def __init__(self, count=3):
        self.values = [0.25] * count
        self.writes = []
        self.name = "Test plugin"

    def operation(self, method, args):
        if method == "isValid":
            assert args == [7, -1, True]
            return True
        if method == "getPluginName":
            assert args == [7, -1, False, True]
            return self.name
        if method == "getParamCount":
            return len(self.values)
        if method == "getParamName":
            assert args[1:] == [7, -1, True]
            return f"Param {args[0]}"
        if method == "getParamValue":
            assert args[1:] == [7, -1, True]
            return self.values[args[0]]
        if method == "setParamValue":
            assert args[2:] == [7, -1, 0, True]
            self.writes.append(args)
            self.values[args[1]] = args[0]
            return None
        raise AssertionError(method)

    def send(self, action, params):
        if action == "native.call":
            return {"success": True, "result": self.operation(params["method"], params["args"])}
        assert action == "native.batch" and len(params["operations"]) <= 128
        return {
            "success": True,
            "results": [
                {"result": self.operation(op["method"], op["args"])} for op in params["operations"]
            ],
        }


def test_capture_paginates_every_public_parameter_and_restore_changes_only():
    rpc = RPC(145)
    plugin = PluginParameters(rpc)
    state = plugin.capture_all(7)
    assert state["complete"] and len(state["parameters"]) == 145
    rpc.values[130] = 0.6
    restored = plugin.restore(state)
    assert restored["verified"] == [130]
    assert len(rpc.writes) == 1 and rpc.values[130] == 0.25


@pytest.mark.parametrize("mismatch", ["name", "index", "value", "duplicate"])
def test_all_snapshot_validation_precedes_any_parameter_write(mismatch):
    rpc = RPC()
    plugin = PluginParameters(rpc)
    state = deepcopy(plugin.capture_all(7))
    rpc.values[0] = 0.7
    if mismatch == "name":
        state["parameters"][-1]["name"] = "Different parameter"
    elif mismatch == "index":
        state["parameters"][-1]["index"] = 1000
    elif mismatch == "value":
        state["parameters"][-1]["value"] = float("nan")
    else:
        state["parameters"][-1]["index"] = 0
    with pytest.raises(ValueError):
        plugin.restore(state)
    assert not rpc.writes


def test_replaced_plugin_is_rejected_before_writes():
    rpc = RPC()
    plugin = PluginParameters(rpc)
    state = plugin.capture_all(7)
    rpc.name = "Replacement plugin"
    with pytest.raises(ValueError, match="differs"):
        plugin.restore(state)
    assert not rpc.writes


def test_unused_unnamed_wrapper_slot_does_not_block_named_parameter_restoration():
    class UnnamedRPC(RPC):
        def operation(self, method, args):
            if method == "getParamName" and args[0] == 1:
                return ""
            return super().operation(method, args)

    rpc = UnnamedRPC()
    plugin = PluginParameters(rpc)
    state = plugin.capture_all(7)
    rpc.values[0] = 0.6
    rpc.values[1] = 0.5
    result = plugin.restore(state)
    assert result["verified"] == [0] and result["skipped_unnamed"] == 1
    assert rpc.values[0] == 0.25 and rpc.values[1] == 0.5


def test_restore_to_allows_cross_slot_same_plugin(monkeypatch):
    """restore_to must accept a different location but still check model/count."""
    from fl_studio_mcp.tools.plugin_state import PluginParameters

    pp = PluginParameters.__new__(PluginParameters)
    pp.rpc = object()
    target = {"index": 3, "slot": 1, "plugin_name": "Fruity Balance", "parameter_count": 2}
    monkeypatch.setattr(pp, "identity", lambda i, s: target)
    monkeypatch.setattr(pp, "_write_named",
                        lambda state, identity: {"success": True, "identity": identity})
    state = {
        "schema": "fl-public-plugin-parameters/v1",
        "target": {"index": 3, "slot": 0, "plugin_name": "Fruity Balance",
                   "parameter_count": 2},
        "parameters": [],
    }
    assert pp.restore_to(state, 3, 1)["success"]

    state["target"]["plugin_name"] = "3x Osc"
    try:
        pp.restore_to(state, 3, 1)
        assert False, "different plugin must be rejected"
    except ValueError:
        pass


def test_clone_rejects_same_slot_and_bad_args():
    from fl_studio_mcp.tools.plugin_state import clone_mixer_effect

    assert not clone_mixer_effect(None, 3, 0, 3, 0)["success"]
    assert not clone_mixer_effect(None, -1, 0, 3, 1)["success"]
