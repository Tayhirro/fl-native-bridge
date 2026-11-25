"""Back up and deploy the bridge without editing FL binaries or project data."""

from pathlib import Path
import hashlib
import json
import os
import shutil
import time


def transform(original):
    if "import native_rpc\n" in original:
        return original
    result = original.replace(
        "import arrangement\n",
        "import arrangement\nimport bridge_extension\nimport native_rpc\nimport importlib\nimportlib.reload(bridge_extension)\nimportlib.reload(native_rpc)\n",
        1,
    )
    result = result.replace(
        "    pass  # Required FL Studio API hook; no polling needed",
        "    native_rpc.poll(dispatch_command)",
        1,
    )
    result = result.replace(
        'response = {"success": True, **result}',
        'response = {**result, "success": not bool(result.get("error")) and result.get("success", True)}',
        1,
    )
    result = result.replace(
        "        RESPONSE_FILE.write_text(json.dumps(response, indent=2))",
        "        native_rpc.atomic_json(RESPONSE_FILE, response)",
        1,
    )
    result = result.replace(
        "    # Transport commands\n",
        '    if action.startswith("native."):\n'
        "        return bridge_extension.dispatch(action, params)\n"
        '    if action == "arrangement.listMethods":\n'
        "        return handle_arrangement_list_methods()\n\n    # Transport commands\n",
        1,
    )
    result = result.replace("arrangement.getSelection(True, False)", "arrangement.selectionStart()")
    result = result.replace("arrangement.getSelection(False, True)", "arrangement.selectionEnd()")
    result = result.replace("arrangement.getSelectionStart()", "arrangement.selectionStart()")
    result += """

for _hook_name in ("OnRefresh", "OnDirtyMixerTrack", "OnDirtyChannel", "OnProjectLoad"):
    _hook_previous = globals().get(_hook_name)
    globals()["_previous_" + _hook_name] = getattr(_hook_previous, "_native_original", _hook_previous)

def OnRefresh(flags):
    if callable(_previous_OnRefresh):
        _previous_OnRefresh(flags)
    native_rpc.event("refresh", flags=flags)


def OnDirtyMixerTrack(index):
    if callable(_previous_OnDirtyMixerTrack):
        _previous_OnDirtyMixerTrack(index)
    native_rpc.event("mixer_dirty", index=index)


def OnDirtyChannel(index, flag):
    if callable(_previous_OnDirtyChannel):
        _previous_OnDirtyChannel(index, flag)
    native_rpc.event("channel_dirty", index=index, flag=flag)


def OnProjectLoad(status):
    if callable(_previous_OnProjectLoad):
        _previous_OnProjectLoad(status)
    native_rpc.event("project_load", status=status)

for _hook_name in ("OnRefresh", "OnDirtyMixerTrack", "OnDirtyChannel", "OnProjectLoad"):
    globals()[_hook_name]._native_original = globals()["_previous_" + _hook_name]
"""
    compile(result, "device_FLStudioMCP.py", "exec")
    assert "native_rpc.poll(dispatch_command)" in result
    return result


def write_atomic(path, text):
    temporary = path.with_suffix(path.suffix + ".deploy.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def main():
    root = Path(__file__).resolve().parents[1]
    installed = Path.home() / "Documents/Image-Line/FL Studio/Settings/Hardware/FLStudioMCP"
    backup = root / "backups" / ("native-" + str(time.time_ns()))
    backup.mkdir(parents=True)
    manifest = []
    source = installed / "device_FLStudioMCP.py"
    updated = transform(source.read_text(encoding="utf-8-sig"))
    for target in (source, root / "fl_controller/device_FLStudioMCP.py"):
        manifest.append(
            dict(path=str(target), sha256=hashlib.sha256(target.read_bytes()).hexdigest())
        )
        shutil.copy2(target, backup / ("installed.py" if target == source else "repository.py"))
    for name in ("bridge_extension.py", "native_rpc.py"):
        target = installed / name
        if target.exists():
            shutil.copy2(target, backup / name)
        write_atomic(target, (root / "fl_controller" / name).read_text(encoding="utf-8"))
    for target in (source, root / "fl_controller/device_FLStudioMCP.py"):
        write_atomic(target, updated)
    server = root / "src/fl_studio_mcp/server.py"
    original = server.read_text(encoding="utf-8")
    if "register_native_tools(mcp)" not in original:
        shutil.copy2(server, backup / "server.py")
        original = original.replace(
            "from fl_studio_mcp.utils.connection import",
            "from fl_studio_mcp.tools.native_tools import register_native_tools\nfrom fl_studio_mcp.utils.connection import",
            1,
        )
        original = original.replace(
            "register_arrangement_tools(mcp)\n",
            "register_arrangement_tools(mcp)\nregister_native_tools(mcp)\n",
            1,
        )
        original = original.replace(
            "1. Cannot load new VST/AU plugins - only control existing ones",
            "1. Native calls, window actions and file/preset drops are available through native and desktop tools",
        )
        original = original.replace(
            "2. Cannot create new patterns programmatically",
            "2. Pattern clone/name/length operations are available through native calls",
        )
        write_atomic(server, original)
    if "register_desktop_tools(mcp)" not in original:
        original = original.replace(
            "from fl_studio_mcp.utils.connection import",
            "from fl_studio_mcp.tools.desktop import register_desktop_tools\nfrom fl_studio_mcp.utils.connection import",
            1,
        )
        original = original.replace(
            "register_native_tools(mcp)\n",
            "register_native_tools(mcp)\nregister_desktop_tools(mcp)\n",
            1,
        )
        write_atomic(server, original)
    connection = root / "src/fl_studio_mcp/utils/midi_connection.py"
    original = connection.read_text(encoding="utf-8")
    if "def _send_legacy_command(" not in original:
        shutil.copy2(connection, backup / "midi_connection.py")
        start = original.index(
            "        self.ensure_connected()\n", original.index("    def send_command(")
        )
        original = (
            original[:start]
            + '''        from fl_studio_mcp.utils.file_rpc import FileRPC

        rpc = FileRPC(self._hardware_dir / "native_rpc")
        if rpc.heartbeat().get("healthy") or list(rpc.base.glob("*/heartbeat.json")):
            return rpc.send(action, params, max(timeout, 10.0))
        if action.startswith("native."):
            return {"success": False, "error": "Reload FLStudioMCP to activate native queue v2"}
        return self._send_legacy_command(action, params, timeout)

    def _send_legacy_command(self, action, params=None, timeout=2.0):
        """Compatibility until FL reloads the new controller; not used after queue v2 is active."""
'''
            + original[start:]
        )
        write_atomic(connection, original)
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Installed. Reload FLStudioMCP from the script output window. Backup:", backup)


if __name__ == "__main__":
    main()
