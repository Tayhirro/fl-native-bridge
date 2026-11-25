from contextlib import nullcontext

import pytest

from fl_studio_mcp.tools.plugin_editor import EditorError, PluginEditor


class Host:
    def __init__(self):
        self.names = ["Drive", "Mix", "", "Mode"]
        self.values = [0.25, 0.5, 0.0, 1.0]
        self.writes = []
        self.coupled = False
        self.rejected = None

    def heartbeat(self):
        return {"healthy": True, "pid": 42, "generation": "1"}

    def queue_lock(self, name):
        return nullcontext()

    def send(self, action, params):
        if action == "native.call":
            return {"success": True, "result": self.operation(**params), "request_id": "test"}
        assert action == "native.batch" and len(params["operations"]) <= 128
        return {
            "success": True,
            "results": [{"result": self.operation(**op)} for op in params["operations"]],
        }

    def operation(self, module, method, args):
        if (module, method) == ("channels", "channelCount"):
            assert args == [True]
            return 2
        if (module, method) == ("mixer", "trackCount"):
            return 1
        assert module == "plugins"
        if method == "isValid":
            return args in ([1, -1, True], [0, 9, True])
        if method == "getPluginName":
            return "Instrument" if args[1] < 0 else "Effect"
        if method == "getParamCount":
            return len(self.names)
        if method == "setParamValue":
            value, index, location, slot, pickup, global_index = args
            assert [location, slot, global_index] in ([1, -1, True], [0, 9, True])
            assert pickup == 0
            self.writes.append(index)
            if index != self.rejected:
                self.values[index] = value
            if self.coupled and index == 1:
                self.values[0] = 0.25
            return None
        index, location, slot, global_index = args
        assert [location, slot, global_index] in ([1, -1, True], [0, 9, True])
        return {
            "getParamName": self.names[index],
            "getParamValue": self.values[index],
            "getParamValueString": f"{self.values[index]:.0%}",
        }[method]


def test_discovers_both_target_kinds_and_uses_runtime_display_value_signature():
    editor = PluginEditor(Host())
    assert [p["target"] for p in editor.instances()["instances"]] == ["channel:1", "mixer:0:9"]
    result = editor.describe("mixer:0:9", query="mix", include_text=True, refresh=True)
    assert result["named_parameter_count"] == 3 and result["page"]["next_start"] is None
    assert result["parameters"][0]["value_text"] == "50%"


@pytest.mark.parametrize("target", ["channel:1", "mixer:0:9"])
def test_name_and_index_edits_share_preview_write_and_independent_readback(target):
    host = Host()
    editor = PluginEditor(host)
    request = [
        {"parameter": "  DRiVe ", "value": 0.4, "expected_value": 0.25},
        {"parameter": 1, "value": 0.7, "expected_name": "Mix"},
    ]
    plan = editor.edit(target, request, dry_run=True)
    assert not host.writes and [p["index"] for p in plan["plan"]] == [0, 1]
    result = editor.edit(target, request, expected_fingerprint=plan["plugin"]["fingerprint"])
    assert result["verified_readback"] and host.writes == [0, 1]
    assert host.values[:2] == [0.4, 0.7]


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        ({"parameter": 1, "value": True}, "InvalidValue"),
        ({"parameter": 1, "value": 0.8, "expected_value": 0.1}, "ValueChanged"),
        ({"parameter": 1, "value": 0.8, "expected_name": 5}, "InvalidParameter"),
        ({"parameter": 1, "value": 0.8, "expected_name": "Drive"}, "ParameterChanged"),
        ({"parameter": 2, "value": 0.8}, "UnnamedParameter"),
    ],
)
def test_entire_group_is_preflighted_before_writes(edit, error):
    host = Host()
    with pytest.raises(EditorError) as failure:
        PluginEditor(host).edit("channel:1", [{"parameter": 0, "value": 0.4}, edit])
    assert failure.value.code == error and not host.writes


def test_name_write_rereads_metadata_and_refuses_new_duplicate():
    host = Host()
    editor = PluginEditor(host)
    editor.describe("channel:1", refresh=True)
    host.names[1] = "DRIVE"
    with pytest.raises(EditorError) as failure:
        editor.edit("channel:1", [{"parameter": "Drive", "value": 0.4}])
    assert failure.value.code == "AmbiguousParameter" and not host.writes


def test_rejected_setter_is_not_retried_and_reports_partial_group():
    host = Host()
    host.rejected = 1
    result = PluginEditor(host).edit(
        "channel:1", [{"parameter": 0, "value": 0.4}, {"parameter": 1, "value": 0.7}]
    )
    assert result["error_type"] == "ValueNotConfirmed" and result["write_may_have_executed"]
    assert [edit["index"] for edit in result["applied"]] == [0] and host.writes == [0, 1]


def test_group_reports_when_later_parameter_mutation_changes_first_value():
    host = Host()
    host.coupled = True
    result = PluginEditor(host).edit(
        "channel:1", [{"parameter": 0, "value": 0.4}, {"parameter": 1, "value": 0.7}]
    )
    assert result["error_type"] == "GroupValueNotConfirmed"
    assert result["details"]["mismatches"] == [{"index": 0, "requested": 0.4, "observed": 0.25}]
