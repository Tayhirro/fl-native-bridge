"""Install the typed native bridge alongside the existing FL controller."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path


def replace_once(text: str, before: str, after: str) -> str:
    if text.count(before) != 1:
        raise ValueError("Installed controller does not match the expected bridge layout")
    return text.replace(before, after, 1)


def integrate_controller(original: str) -> str:
    if "import semantic_native\n" in original:
        if "semantic_native.playlist_refreshed()" in original:
            return original
        return replace_once(
            original,
            '    native_rpc.event("refresh", flags=flags)\n',
            "    if semantic_native is not None:\n"
            "        semantic_native.playlist_refreshed()\n"
            '    native_rpc.event("refresh", flags=flags)\n',
        )
    result = replace_once(
        original,
        "importlib.reload(native_rpc)\n",
        "importlib.reload(native_rpc)\n"
        "try:\n"
        "    import semantic_native\n"
        "    importlib.reload(semantic_native)\n"
        "except ImportError:\n"
        "    semantic_native = None\n",
    )
    result = replace_once(
        result, "TRIGGER_NOTE = 127\n", "TRIGGER_NOTE = 127\n_native_idle_active = False\n"
    )
    result = replace_once(
        result,
        'def OnIdle():\n    """Called periodically when FL Studio is idle."""\n'
        '    native_rpc.poll(dispatch_command)\n',
        'def OnIdle():\n    """Called periodically when FL Studio is idle."""\n'
        "    global _native_idle_active\n"
        "    _native_idle_active = True\n"
        "    try:\n"
        "        native_rpc.poll(dispatch_command)\n"
        "    finally:\n"
        "        _native_idle_active = False\n",
    )
    result = replace_once(
        result,
        '    if action.startswith("native."):\n',
        "    if semantic_native is not None and action in semantic_native.ACTIONS:\n"
        "        if not _native_idle_active:\n"
        '            return {"success": False, "error": '
        '"This operation requires the FL idle queue"}\n'
        "        return semantic_native.dispatch(action, params)\n"
        '    if action.startswith("native."):\n',
    )
    result = replace_once(
        result,
        '    native_rpc.event("project_load", status=status)\n',
        "    if semantic_native is not None:\n"
        "        semantic_native.project_changed()\n"
        '    native_rpc.event("project_load", status=status)\n',
    )
    result = replace_once(
        result,
        '    native_rpc.event("refresh", flags=flags)\n',
        "    if semantic_native is not None:\n"
        "        semantic_native.playlist_refreshed()\n"
        '    native_rpc.event("refresh", flags=flags)\n',
    )
    compile(result, "device_FLStudioMCP.py", "exec")
    return result


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "fl_controller"
    target = Path.home() / "Documents/Image-Line/FL Studio/Settings/Hardware/FLStudioMCP"
    installed = target / "device_FLStudioMCP.py"
    original = installed.read_bytes()
    updated = integrate_controller(original.decode("utf-8-sig").replace("\r\n", "\n"))
    repository = (source / "device_FLStudioMCP.py").read_text(encoding="utf-8-sig")
    if updated.replace("\r\n", "\n") != repository.replace("\r\n", "\n"):
        raise RuntimeError("Installed and repository controllers differ; installation stopped")

    contents = {
        "semantic_native.py": (source / "semantic_native.py").read_bytes(),
        "_flmcp_native.pyd": (source / "_flmcp_native.pyd").read_bytes(),
        "_flmcp_native_20261121.pyd": (source / "_flmcp_native_20261121.pyd").read_bytes(),
        "_flmcp_native_20261121_points2.pyd": (
            source / "_flmcp_native_20261121_points2.pyd"
        ).read_bytes(),
        "_flmcp_native_20261121_links.pyd": (
            source / "_flmcp_native_20261121_links.pyd"
        ).read_bytes(),
        "_flmcp_native_20261121_sampler.pyd": (
            source / "_flmcp_native_20261121_sampler.pyd"
        ).read_bytes(),
        "device_FLStudioMCP.py": updated.encode("utf-8"),
    }
    backup = root / "backups" / ("semantic-native-" + str(time.time_ns()))
    backup.mkdir(parents=True)
    manifest = {}
    for name in contents:
        path = target / name
        if path.exists():
            old = path.read_bytes()
            (backup / name).write_bytes(old)
            manifest[name] = hashlib.sha256(old).hexdigest()
        else:
            manifest[name] = None
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    written = []
    try:
        for name, content in contents.items():
            path = target / name
            if name == "device_FLStudioMCP.py" and path.read_bytes() != original:
                raise RuntimeError("Installed controller changed during deployment")
            if path.exists() and path.read_bytes() == content:
                continue
            temporary = path.with_name(path.name + ".semantic-deploy.tmp")
            temporary.write_bytes(content)
            os.replace(temporary, path)
            written.append(name)
    except Exception:
        for name in reversed(written):
            path = target / name
            saved = backup / name
            if saved.exists():
                shutil.copy2(saved, path)
            elif path.read_bytes() == contents[name]:
                path.unlink()
        raise
    print(f"Installed typed native bridge. Backup: {backup}")
    print("Reload the FLStudioMCP controller to activate the new native operations.")


if __name__ == "__main__":
    main()
