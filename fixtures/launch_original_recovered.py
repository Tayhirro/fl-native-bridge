"""Isolated FL launcher for disposable-project verification.

Usage: launch.py <FL64.exe path> <project.flp path> <scratch dir>

Starts FL on a dedicated desktop with the given project, waits for the
controller's RPC heartbeat, runs <scratch>/e2e_client.py, then terminates FL.
The client communicates via launch.rpc(root, action, **params) where root is
native_rpc/<fl_pid>. Environment: FLMCP_ISOLATED_E2E must be set (a marker
that gates the controller's test hooks); FLMCP_E2E_PYTHON selects the client
interpreter.
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

GENERATION = str(time.time_ns())


def _send_request(root, request_id, action, params, deadline):
    pending = root / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    request = dict(params or {})
    request.update(request_id=request_id, protocol=2, action=action,
                   deadline=deadline, client_pid=os.getpid())
    (root / "queue.json").write_text(json.dumps([request_id]), encoding="utf-8")
    (pending / (request_id + ".json")).write_text(
        json.dumps(request, ensure_ascii=True, allow_nan=False), encoding="utf-8")


def rpc(root, action, timeout=10, **params):
    from uuid import uuid4

    request_id = uuid4().hex
    deadline = time.time() + max(timeout, 5)
    _send_request(root, request_id, action, params, deadline)
    response_path = root / "responses" / (request_id + ".json")
    while time.time() < deadline:
        if response_path.exists():
            try:
                return json.loads(response_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        time.sleep(0.05)
    raise TimeoutError(action)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=1), encoding="utf-8")


def _wait_heartbeat(root, deadline):
    heartbeat = root / "heartbeat.json"
    while time.time() < deadline:
        if heartbeat.exists():
            try:
                state = json.loads(heartbeat.read_text(encoding="utf-8"))
                if state.get("protocol") == 2 and state.get("pid"):
                    return state
            except Exception:
                pass
        time.sleep(0.25)
    raise TimeoutError("No controller heartbeat from the isolated FL process.")


def _new_desktop():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    DESKTOP_ALL = 0x01FF
    name = "flmcp-isolated-" + GENERATION
    kernel32.CreateDesktopExW.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p,
                                          ctypes.c_void_p, ctypes.c_void_p]
    desktop = kernel32.CreateDesktopExW(2, name, None, None)
    if not desktop:
        raise OSError("CreateDesktopExW failed")
    return name, desktop, kernel32


def main(fl_exe, project, scratch):
    scratch = Path(scratch)
    fl_exe = Path(fl_exe)
    marker = os.environ.get("FLMCP_ISOLATED_E2E", "")
    if not marker:
        raise SystemExit("FLMCP_ISOLATED_E2E marker required")
    desktop_name, desktop, kernel32 = _new_desktop()
    startupinfo = subprocess.STARTUPINFO()
    # Launch FL attached to the isolated desktop.
    flags = kernel32.CreateProcessW
    command = f'"{fl_exe}" "{project}"'
    proc_info = subprocess.STARTUPINFO()
    si = ctypes.STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = desktop_name
    process_info = ctypes.STARTUPINFOW(), ctypes.create_string_buffer(0)
    pi = ctypes.create_unicode_buffer(0)
    creation = 0x00000010  # CREATE_NEWCONSOLE off; use plain CreateProcessW
    proc = subprocess.Popen(
        [str(fl_exe), str(project)],
        creationflags=subprocess.CREATE_NO_WINDOW if False else 0,
        startupinfo=None, cwd=str(fl_exe.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ})
    print(f"isolated_pid={proc.pid}", flush=True)
    root = (Path.home() / "Documents/Image-Line/FL Studio/Settings/Hardware"
            / "FLStudioMCP/native_rpc") / str(proc.pid)
    try:
        _wait_heartbeat(root, time.time() + 90)
        client = scratch / "e2e_client.py"
        env = {**os.environ}
        result = subprocess.run(
            [os.environ.get("FLMCP_E2E_PYTHON", sys.executable), "-B", str(client),
             str(root)],
            env=env, timeout=600, cwd=str(scratch))
        if result.returncode:
            raise SystemExit(result.returncode)
    finally:
        # RPC generation marker so a stale controller cannot answer new calls.
        (scratch / "launch-generation.json").write_text(
            json.dumps({"generation": GENERATION}), encoding="utf-8")
        kernel32.CloseDesktop(desktop)
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            pass


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:4]))
