import importlib.util
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fl_studio_mcp.utils.file_rpc import FileRPC

ROOT = Path(__file__).parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "fl_controller" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def consumer(tmp_path):
    module = load("test_native_rpc_server", "native_rpc.py")
    module.ROOT = tmp_path / "123"
    client = FileRPC(tmp_path, pid=123)
    module.poll(lambda action, params: {"value": params["value"]})
    yield module, client


def test_concurrent_requests_are_correlated(consumer):
    server, client = consumer
    done = threading.Event()
    executed = []

    def dispatch(action, params):
        executed.append(params["value"])
        return {"value": params["value"]}

    def consume():
        while not done.is_set():
            server.poll(dispatch)
            time.sleep(0.005)

    thread = threading.Thread(target=consume)
    thread.start()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda i: client.send("echo", {"value": i}, 5), range(40)))
    finally:
        done.set()
        thread.join()
    assert [response["value"] for response in responses] == list(range(40))
    assert len({response["request_id"] for response in responses}) == 40
    assert sorted(executed) == list(range(40))


def test_duplicate_request_does_not_replay_mutation(consumer):
    server, client = consumer
    request_id = "a" * 32
    path = server.ROOT / "pending" / (request_id + ".json")
    value = dict(
        protocol=2, request_id=request_id, deadline=time.time() + 10, action="mutate", params={}
    )
    path.write_text(json.dumps(value))
    client.refresh_queue()
    calls = []
    server._last_poll = 0
    server.poll(lambda *_: calls.append(1) or {})
    path.write_text(json.dumps(value))
    server._last_poll = 0
    server.poll(lambda *_: calls.append(1) or {})
    assert calls == [1]
    assert client.status(request_id)["state"] == "completed"


def test_expired_request_never_executes(consumer):
    server, client = consumer
    request_id = "b" * 32
    path = server.ROOT / "pending" / (request_id + ".json")
    path.write_text(
        json.dumps(dict(protocol=2, request_id=request_id, deadline=0, action="mutate", params={}))
    )
    client.refresh_queue()
    calls = []
    server._last_poll = 0
    server.poll(lambda *_: calls.append(1) or {})
    assert not calls
    assert client.status(request_id)["response"]["error_type"] == "TimeoutError"


def test_error_handler_is_not_success(consumer):
    server, client = consumer
    request_id = "c" * 32
    (server.ROOT / "pending" / (request_id + ".json")).write_text(
        json.dumps(
            dict(
                protocol=2,
                request_id=request_id,
                deadline=time.time() + 10,
                action="bad",
                params={},
            )
        )
    )
    client.refresh_queue()
    server._last_poll = 0
    server.poll(lambda *_: {"error": "invalid"})
    assert client.status(request_id)["response"]["success"] is False


def test_inflight_request_after_reload_is_not_replayed(consumer):
    server, client = consumer
    request_id = "d" * 32
    (server.ROOT / "inflight").mkdir(exist_ok=True)
    (server.ROOT / "inflight" / (request_id + ".json")).write_text("{}")
    (server.ROOT / "pending" / (request_id + ".json")).write_text("{}")
    client.refresh_queue()
    calls = []
    server._last_poll = 0
    server.poll(lambda *_: calls.append(1) or {})
    assert not calls
    assert client.status(request_id)["state"] == "outcome_unknown"


def test_request_id_cannot_escape_queue_directory(consumer):
    _, client = consumer
    with pytest.raises(ValueError):
        client.status("../heartbeat")


def test_missing_controller_does_not_publish_commands(tmp_path):
    client = FileRPC(tmp_path, pid=123)
    response = client.send("mutate", {}, 0.1)
    assert response["error_type"] == "ControllerUnavailable"
    assert not list((client.root / "pending").glob("*.json"))


def test_native_unsafe_call_is_rejected():
    from types import SimpleNamespace

    bridge = load("test_fl_bridge", "bridge_extension.py")
    calls = []
    modules = {
        "general": SimpleNamespace(safeToEdit=lambda: False),
        "patterns": SimpleNamespace(clonePattern=lambda *_: calls.append(1)),
    }
    with pytest.raises(RuntimeError, match="safeToEdit"):
        bridge.dispatch(
            "native.call", {"module": "patterns", "method": "clonePattern", "args": [1]}, modules
        )
    assert not calls


def test_native_private_method_is_rejected():
    bridge = load("test_fl_bridge", "bridge_extension.py")
    with pytest.raises(ValueError):
        bridge.dispatch(
            "native.call", {"module": "os", "method": "system", "args": ["anything"]}, {}
        )
    assert bridge.is_read("findFirstNextEmptyPat") is False


def test_unknown_native_action_never_reaches_an_effect_write():
    bridge = load("test_fl_bridge", "bridge_extension.py")
    with pytest.raises(ValueError, match="Unknown native action"):
        bridge.dispatch("native.typo", {"index": 1, "slot": 0, "level": 0.5}, {})


def test_full_state_uses_global_channels_and_nonoverlapping_playlist_pages():
    from types import SimpleNamespace

    bridge = load("test_fl_bridge", "bridge_extension.py")
    channels = SimpleNamespace(
        channelCount=lambda global_count=False: 40 if global_count else 2,
        getChannelName=lambda i, global_index: str(i) if global_index else "wrong group",
        getChannelVolume=lambda i, mode, global_index: 0.5 if global_index and not mode else -1,
        getTargetFxTrack=lambda i, global_index: i if global_index else -1,
        selectedChannel=lambda can_be_none, offset, global_index: 39 if global_index else 1,
    )
    modules = {name: SimpleNamespace() for name in ("ui", "general", "transport")}
    modules.update(
        channels=channels,
        mixer=SimpleNamespace(trackCount=lambda: 1),
        playlist=SimpleNamespace(trackCount=lambda: 500),
        patterns=SimpleNamespace(patternCount=lambda: 6),
    )
    first = bridge.dispatch("native.snapshot", {"start": 0, "limit": 32}, modules)
    second = bridge.dispatch("native.snapshot", {"start": 32, "limit": 32}, modules)
    assert first["channel_count"] == 40
    assert first["selected_channel"] == 39
    assert first["channels"][0]["volume"] == 0.5
    assert second["channels"][0]["name"] == "32"
    assert [item["index"] for item in first["playlist"]] == list(range(1, 33))
    assert [item["index"] for item in second["playlist"]] == list(range(33, 65))


def test_native_batch_reports_partial_failure():
    from types import SimpleNamespace

    bridge = load("test_fl_bridge", "bridge_extension.py")
    response = bridge.dispatch(
        "native.batch",
        {
            "operations": [
                {"module": "mixer", "method": "trackCount"},
                {"module": "mixer", "method": "notAvailable"},
            ]
        },
        {"mixer": SimpleNamespace(trackCount=lambda: 125)},
    )
    assert response["success"] is False
    assert response["failed_index"] == 1
    assert response["completed"][0]["result"] == 125


def test_multi_process_clients_do_not_cross_responses(consumer):
    server, client = consumer
    done = threading.Event()

    def consume():
        while not done.is_set():
            server.poll(lambda action, params: {"value": params["value"]})
            time.sleep(0.005)

    thread = threading.Thread(target=consume)
    thread.start()
    program = (
        "import sys,json; from pathlib import Path; "
        "from fl_studio_mcp.utils.file_rpc import FileRPC; "
        "r=FileRPC(Path(sys.argv[1]),pid=123); "
        'print(json.dumps([r.send("echo",{"value":int(sys.argv[2])+i},5) for i in range(5)]))'
    )
    try:
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", program, str(client.base), str(base)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for base in (0, 100, 200, 300)
        ]
        for process, base in zip(processes, (0, 100, 200, 300)):
            out, error = process.communicate(timeout=45)
            assert process.returncode == 0, error
            assert [response["value"] for response in json.loads(out)] == list(
                range(base, base + 5)
            )
    finally:
        done.set()
        thread.join()


def test_event_overflow_reports_gap(consumer):
    server, _ = consumer
    for i in range(300):
        server.event("test", index=i)
    state = server.events_since(0)
    assert len(state["events"]) == 256
    assert state["history_gap"] is True


def test_foreign_window_is_rejected(monkeypatch):
    from types import SimpleNamespace

    from fl_studio_mcp.utils import desktop

    gui = SimpleNamespace(IsWindow=lambda _: True)
    process = SimpleNamespace(GetWindowThreadProcessId=lambda _: (10, 456))
    monkeypatch.setattr(desktop, "_win", lambda: (gui, process))
    with pytest.raises(ValueError, match="different process"):
        desktop.require_window(100, pid=123)


def test_typed_arguments_support_sysex_bytes_and_fl_constants():
    from types import SimpleNamespace

    bridge = load("test_fl_bridge", "bridge_extension.py")
    calls = []
    modules = {
        "general": SimpleNamespace(safeToEdit=lambda: True),
        "device": SimpleNamespace(midiOutSysex=lambda value: calls.append(value)),
        "midi": SimpleNamespace(TEST=123),
    }
    bridge.dispatch(
        "native.call",
        {"module": "device", "method": "midiOutSysex", "args": [{"$bytes": "8AH3"}]},
        modules,
    )
    assert calls == [bytes([0xF0, 1, 0xF7])]
    assert bridge.decode({"$constant": "midi.TEST"}, modules.__getitem__) == 123


def test_controlled_window_geometry_roundtrip(monkeypatch):
    import os

    import win32api
    import win32con
    import win32gui

    from fl_studio_mcp.utils import desktop

    class_name = "TFLBridgeValidationForm"
    window_class = win32gui.WNDCLASS()
    window_class.hInstance = win32api.GetModuleHandle(None)
    window_class.lpszClassName = class_name
    window_class.lpfnWndProc = {win32con.WM_DESTROY: lambda *_: 0}
    atom = win32gui.RegisterClass(window_class)
    hwnd = win32gui.CreateWindowEx(
        0x08000080,
        atom,
        "FL bridge offline validation",
        0x80000000 | 0x10000000,
        -10000,
        -10000,
        200,
        100,
        0,
        0,
        window_class.hInstance,
        None,
    )
    monkeypatch.setattr(desktop, "target_pid", os.getpid)
    try:
        result = desktop.move(hwnd, -9500, -9500, 300, 150)
        assert result["verified_position"]
        assert result["after"] == [-9500, -9500, -9200, -9350]
    finally:
        win32gui.DestroyWindow(hwnd)
        win32gui.UnregisterClass(atom, window_class.hInstance)


def test_controller_deployment_preserves_existing_callbacks(monkeypatch):
    import sys
    from types import ModuleType

    spec = importlib.util.spec_from_file_location(
        "test_installer", ROOT / "scripts/install_native_bridge.py"
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    source = (ROOT / "fl_controller/device_FLStudioMCP.py").read_text(encoding="utf-8")
    # Normalize an already installed bridge so the transform is exercised on any checkout.
    source = source.split('\nfor _hook_name in ("OnRefresh",', 1)[0]
    source = source.replace("import native_rpc\n", "").replace("import bridge_extension\n", "")
    source = source.replace("importlib.reload(bridge_extension)\n", "").replace(
        "importlib.reload(native_rpc)\n", ""
    )
    source = source.replace(
        "    native_rpc.poll(dispatch_command)",
        "    pass  # Required FL Studio API hook; no polling needed",
    )
    source += "\ncallback_log = []\ndef OnRefresh(flags):\n    callback_log.append(flags)\n"
    for name in (
        "channels",
        "mixer",
        "plugins",
        "transport",
        "arrangement",
        "bridge_extension",
        "native_rpc",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    events = []
    monkeypatch.setattr(importlib, "reload", lambda module: module)
    sys.modules["native_rpc"].event = lambda *args, **kwargs: events.append((args, kwargs))
    namespace = {"__name__": "test_controller"}
    result = installer.transform(source)
    exec(compile(result, "test_controller.py", "exec"), namespace)
    namespace["OnRefresh"](16)
    assert namespace["callback_log"] == [16]
    assert events == [(("refresh",), {"flags": 16})]
    assert installer.transform(result) == result


def test_filesystem_failure_does_not_spam_fl_idle_callback(consumer, monkeypatch):
    server, _ = consumer
    calls = []

    def denied(*_):
        calls.append(1)
        raise PermissionError("test lock")

    monkeypatch.setattr(server, "atomic_json", denied)
    server._last_heartbeat = 0
    server._last_poll = 0
    server.poll(lambda *_: {})
    server._last_poll = 0
    server.poll(lambda *_: {})
    assert calls == [1]
    assert server._retry_after > time.monotonic()


def test_fl_host_does_not_need_filesystem_mutations(consumer, monkeypatch):
    server, client = consumer
    request_id = "f" * 32
    (server.ROOT / "pending" / (request_id + ".json")).write_text(
        json.dumps(
            dict(
                protocol=2,
                request_id=request_id,
                deadline=time.time() + 10,
                action="echo",
                params={"value": 42},
            )
        )
    )
    client.refresh_queue()

    def denied(*args, **kwargs):
        raise SystemError("mkdir returned NULL without setting an exception")

    with monkeypatch.context() as context:
        context.setattr(Path, "mkdir", denied)
        context.setattr(Path, "unlink", denied)
        context.setattr(Path, "glob", denied)
        context.setattr(server.os, "replace", denied)
        server._last_poll = 0
        server._last_heartbeat = 0
        server.poll(lambda action, params: params)
    assert client.status(request_id)["response"]["value"] == 42


def test_partial_host_response_is_retried(consumer, monkeypatch):
    server, client = consumer
    write = server.atomic_json

    def partial_write(path, value):
        if path.parent.name == "responses":
            path.write_text('{"success":', encoding="utf-8")
            time.sleep(0.04)
        write(path, value)

    monkeypatch.setattr(server, "atomic_json", partial_write)
    done = threading.Event()

    def consume():
        while not done.is_set():
            server.poll(lambda _, params: params)
            time.sleep(0.005)

    thread = threading.Thread(target=consume)
    thread.start()
    try:
        response = client.send("echo", {"value": 42}, 2)
        assert response["success"] and response["value"] == 42
    finally:
        done.set()
        thread.join()


def test_ole_timeout_is_ambiguous_and_never_retried(monkeypatch):
    import subprocess

    from fl_studio_mcp.utils import desktop

    calls = []
    monkeypatch.setattr(desktop, "require_window", lambda _: (None, None, 123))

    def timeout(*args, **kwargs):
        calls.append(1)
        assert kwargs["stdin"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(
            args[0], kwargs["timeout"], stderr=b'{"phase":"drag_loop_enter","time":1}\n'
        )

    monkeypatch.setattr(subprocess, "run", timeout)
    result = desktop.drop_file(100, "test.wav", 100, 100)
    assert calls == [1]
    assert result["submitted"] is None
    assert result["error_type"] == "OutcomeUnknown"
    assert result["requires_native_readback"] is True
    assert result["last_phase"] == "drag_loop_enter"
    assert result["helper_trace"][0]["time"] == 1


def test_ole_preparation_never_focuses_or_enters_drag_loop(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fl_studio_mcp.utils import desktop

    # A real Shell IDataObject is prepared, but no foreground query, cursor
    # movement, or DoDragDrop is allowed to reach a real app.
    monkeypatch.setattr(
        desktop, "require_window", lambda _: (SimpleNamespace(IsWindowVisible=lambda _: True), None, 123)
    )

    def unexpected(*args, **kwargs):
        raise AssertionError("Preparation must not focus or start dragging")

    monkeypatch.setattr(desktop, "focus", unexpected)
    monkeypatch.setattr(desktop, "_run_ole_drop", unexpected)
    file = tmp_path / "prepare-only.wav"
    file.write_bytes(b"local preflight fixture")
    trace = []
    result = desktop._drop_file_in_process(
        100, str(file), 10, 10, prepare_only=True, trace=lambda phase, **_: trace.append(phase)
    )
    assert result == {"success": True, "prepared": True, "submitted": False, "mouse_moved": False}
    assert "shell_data_ready" in trace
    assert "drop_target_check" not in trace
    assert trace[-1] == "helper_completed"


def test_background_ole_loop_receives_local_wakeup_and_cancels(tmp_path):
    import ctypes

    import pythoncom
    import winerror
    from win32com.server.util import wrap
    from win32com.shell import shell, shellcon

    from fl_studio_mcp.utils.desktop import _run_ole_drop

    # No global input, cursor motion or accepted drop: only our COM thread queue.
    class CancelSource:
        _com_interfaces_ = [pythoncom.IID_IDropSource]
        _public_methods_ = ["QueryContinueDrag", "GiveFeedback"]

        def QueryContinueDrag(self, escape, keys):
            return winerror.DRAGDROP_S_CANCEL

        def GiveFeedback(self, effect):
            return winerror.DRAGDROP_S_USEDEFAULTCURSORS

    file = tmp_path / "cancel-only.txt"
    file.write_text("OLE cancellation regression")
    pythoncom.OleInitialize()
    try:
        pidl = shell.SHParseDisplayName(str(file), 0)[0]
        parent = shell.SHParseDisplayName(str(file.parent), 0)[0]
        data = shell.SHCreateDataObject(parent, [pidl[-1:]], None, pythoncom.IID_IDataObject)
        source = wrap(CancelSource(), pythoncom.IID_IDropSource)
        start = time.monotonic()
        assert _run_ole_drop(data, source, shellcon.DROPEFFECT_COPY) == 0
        assert time.monotonic() - start < 2
    finally:
        ctypes.windll.ole32.OleUninitialize()


def test_legacy_rec_adapter_is_disabled_after_host_crash(monkeypatch):
    import ast
    from types import SimpleNamespace

    source = ast.parse((ROOT / "fl_controller/device_FLStudioMCP.py").read_text(encoding="utf-8"))
    node = next(
        n
        for n in source.body
        if isinstance(n, ast.FunctionDef) and n.name == "handle_plugins_set_param_rec"
    )
    calls = []

    def native_rec(event, value, flags):
        # Model the real C argument parser which rejects a float for value.
        if type(value) is not int:
            raise TypeError("'float' object cannot be interpreted as an integer")
        calls.append((event, value, flags))

    general = SimpleNamespace(processRECEvent=native_rec, safeToEdit=lambda: True)
    monkeypatch.setitem(sys.modules, "general", general)
    monkeypatch.setitem(
        sys.modules, "midi", SimpleNamespace(FromMIDI_Max=2**30, REC_MIDIController=1013)
    )
    namespace = {
        "mixer": SimpleNamespace(trackCount=lambda: 31, getTrackPluginId=lambda *_: 1000),
        "plugins": SimpleNamespace(
            isValid=lambda *_: True,
            getParamCount=lambda *_: 2,
            getParamValue=lambda *_: 0.25,
            getParamName=lambda *_: "Test",
            getParamValueString=lambda *_: "25%",
        ),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "effect_rec.py", "exec"), namespace)
    handler = namespace["handle_plugins_set_param_rec"]
    request = dict(track=30, slot_index=0, param_index=1, value=0.25)
    assert handler(request)["disabled"]
    assert handler(request)["error_type"] == "DisabledAfterCrash"
    assert calls == []


def test_raw_rec_call_cannot_bypass_the_disabled_adapter():
    bridge = load("test_fl_bridge", "bridge_extension.py")
    with pytest.raises(RuntimeError, match="Disabled after"):
        bridge.dispatch(
            "native.call",
            {"module": "general", "method": "processRECEvent", "args": [0x06890000, 1, 1013]},
            {},
        )


def test_effect_parameter_uses_public_api_and_verifies_in_later_callback(monkeypatch):
    from fl_studio_mcp.tools.native_tools import register_native_tools
    from fl_studio_mcp.utils import connection

    tools = {}

    class Registry:
        def tool(self):
            def register(function):
                tools[function.__name__] = function
                return function

            return register

    commands = []
    reads = 0

    class Connection:
        def send_command(self, action, params):
            nonlocal reads
            commands.append((action, params))
            assert action != "plugins.setParamRec"
            assert params.get("method") != "processRECEvent"
            method = params["method"]
            if method == "trackCount":
                value = 28
            elif method == "isValid":
                value = True
            elif method == "getParamCount":
                value = 36
            elif method == "setParamValue":
                assert params["args"] == [0.6, 0, 26, 9, 0, True]
                value = None
            elif method == "getParamValue":
                reads += 1
                value = 0.5 if reads == 1 else 0.6
            else:
                raise AssertionError(method)
            return {"success": True, "result": value}

    monkeypatch.setattr(connection, "get_connection", Connection)
    register_native_tools(Registry())
    result = tools["fl_set_effect_parameter"](26, 9, 0, 0.6)
    assert result["success"] and result["verified_readback"]
    assert result["value"] == 0.6 and reads == 2
    assert len([p for _, p in commands if p.get("method") == "setParamValue"]) == 1


@pytest.mark.parametrize("reordered", [False, True])
def test_audio_import_uses_global_indices_and_skips_changed_order(tmp_path, monkeypatch, reordered):
    from fl_studio_mcp.tools.desktop import register_desktop_tools
    from fl_studio_mcp.utils import desktop, file_rpc

    registered = {}

    class ToolRegistry:
        def tool(self):
            def register(function):
                registered[function.__name__] = function
                return function

            return register

    counts = iter([1, 2])
    name_batches = iter([["old"], ["new", "old"] if reordered else ["old", "new"]])
    writes = []

    class RPC:
        def send(self, action, params):
            if action == "native.batch":
                for operation in params["operations"]:
                    assert operation["args"][1] is True
                return {"success": True, "results": [{"result": n} for n in next(name_batches)]}
            module, method, args = params["module"], params["method"], params["args"]
            if method == "channelCount":
                assert args == [True]
                value = next(counts)
            elif module == "mixer" and method == "trackCount":
                value = 31
            elif method == "setTargetFxTrack":
                assert args == [1, 5, True]
                writes.append(args)
                value = None
            elif method == "getTargetFxTrack":
                assert args == [1, True]
                value = 5
            elif method == "getChannelName":
                assert args == [1, True]
                value = "new"
            else:
                raise AssertionError((method, args))
            return {"success": True, "result": value}

    monkeypatch.setattr(file_rpc, "FileRPC", RPC)
    monkeypatch.setattr(desktop, "drop_file", lambda *args: {"submitted": True})
    file = tmp_path / "new.wav"
    file.write_bytes(b"fixture")
    register_desktop_tools(ToolRegistry())
    result = registered["fl_import_audio"](str(file), 100, 10, 10, mixer_track=5)
    if reordered:
        assert result["success"] is False
        assert not writes
    else:
        assert result["verified_new_channels"] and result["verified_routing"]
        assert result["new_channels"][0]["index"] == 1
        assert len(writes) == 1


@pytest.mark.parametrize("matching_name", [True, False])
def test_mixer_parameter_fallback_uses_complete_name_verified_event(monkeypatch, matching_name):
    from fl_studio_mcp.tools.native_tools import register_native_tools
    from fl_studio_mcp.utils import connection

    registered = {}

    class Registry:
        def tool(self):
            def register(function):
                registered[function.__name__] = function
                return function

            return register

    automation = []

    class Connection:
        def send_command(self, action, params):
            assert action == "native.call"
            method = params["method"]
            assert method != "processRECEvent"
            if method == "trackCount":
                value = 31
            elif method == "isValid":
                value = True
            elif method == "getParamCount":
                value = 36
            elif method == "setParamValue":
                value = 1
            elif method == "getParamValue":
                value = 0.6 if automation else 0.25
            elif method == "getTrackPluginId":
                value = 0x05C00000
            elif method == "getParamName":
                value = "Band 1 level"
            elif method == "getEventIDName":
                assert params["args"] == [0x75C08000]
                value = "EQ - Band 1 level" if matching_name else "Unknown event"
            elif method == "automateEvent":
                assert params["args"] == [0x75C08000, round(0.6 * (1 << 30)), 1013]
                automation.append(params["args"])
                value = 1
            else:
                raise AssertionError(method)
            return {"success": True, "result": value}

    monkeypatch.setattr(connection, "get_connection", Connection)
    register_native_tools(Registry())
    result = registered["fl_set_effect_parameter"](23, 0, 0, 0.6, allow_mixer_fallback=True)
    if matching_name:
        assert result["success"] and result["verified_readback"]
        assert result["event_id"] == "0x75c08000" and len(automation) == 1
    else:
        assert not result["success"] and not automation
