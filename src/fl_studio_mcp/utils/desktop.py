"""Windows adapter: process-bound windows, geometry, input, capture and native OLE drops.

Capture uses the target Window DC, never PrintWindow or WM_PRINT on FL's renderer.
No hidden FL forms are shown through Win32; use the native ui API instead.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
from pathlib import Path
import struct
import threading
import time
import zlib

from fl_studio_mcp.utils.file_rpc import FileRPC


def _win():
    if os.name != "nt":
        raise RuntimeError("Desktop operations require Windows")
    import win32gui
    import win32process

    return win32gui, win32process


def executable_path(pid: int) -> Path:
    _win()
    import win32api
    from ctypes import wintypes

    query = ctypes.windll.kernel32.QueryFullProcessImageNameW
    query.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    handle = win32api.OpenProcess(0x1000, False, pid)
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not query(int(handle), 0, buffer, ctypes.byref(length)):
            raise ctypes.WinError()
        return Path(buffer.value)
    finally:
        handle.Close()


@contextlib.contextmanager
def physical_pixels():
    _win()
    setter = ctypes.windll.user32.SetThreadDpiAwarenessContext
    setter.argtypes = [ctypes.c_void_p]
    setter.restype = ctypes.c_void_p
    previous = setter(ctypes.c_void_p(-4))
    try:
        yield
    finally:
        if previous:
            setter(previous)


def target_pid() -> int:
    state = FileRPC().heartbeat()
    if state.get("healthy"):
        return int(state["pid"])
    gui, process = _win()
    candidates = []
    gui.EnumWindows(
        lambda h, _: (
            candidates.append(process.GetWindowThreadProcessId(h)[1])
            if gui.GetClassName(h) == "TFruityLoopsMainForm"
            else None
        ),
        0,
    )
    candidates = sorted(set(candidates))
    selected = FileRPC().root.name
    if selected.isdigit() and int(selected) in candidates:
        return int(selected)
    if len(candidates) != 1:
        raise RuntimeError("Select an FL instance before desktop operations")
    return candidates[0]


def require_window(hwnd: int, pid: int | None = None):
    gui, process = _win()
    if type(hwnd) is not int or not gui.IsWindow(hwnd):
        raise ValueError("Window handle is stale or invalid; refresh the window list")
    expected = pid or target_pid()
    if process.GetWindowThreadProcessId(hwnd)[1] != expected:
        raise ValueError("Window belongs to a different process")
    return gui, process, expected


def windows(include_controls: bool = False, pid: int | None = None) -> dict:
    gui, process = _win()
    expected = pid or target_pid()
    result, seen = [], set()
    with physical_pixels():

        def add(hwnd, _):
            if hwnd in seen or process.GetWindowThreadProcessId(hwnd)[1] != expected:
                return
            seen.add(hwnd)
            name = gui.GetClassName(hwnd)
            if not include_controls and not ("Form" in name or name == "#32770"):
                return
            if len(result) >= 2000:
                return
            result.append(
                dict(
                    hwnd=hwnd,
                    parent=gui.GetParent(hwnd),
                    class_name=name,
                    title=gui.GetWindowText(hwnd),
                    visible=bool(gui.IsWindowVisible(hwnd)),
                    rect=list(gui.GetWindowRect(hwnd)),
                    dpi=ctypes.windll.user32.GetDpiForWindow(hwnd),
                )
            )

        def top(hwnd, _):
            if process.GetWindowThreadProcessId(hwnd)[1] == expected:
                add(hwnd, 0)
                gui.EnumChildWindows(hwnd, add, 0)

        gui.EnumWindows(top, 0)
    return {"pid": expected, "coordinate_space": "physical screen pixels", "windows": result}


def move(hwnd: int, x: int, y: int, width: int | None = None, height: int | None = None) -> dict:
    gui, _, _ = require_window(hwnd)
    movable = {
        "TFruityLoopsMainForm",
        "TEventEditForm",
        "TFXForm",
        "TStepSeqForm",
        "TPluginForm",
        "TSampleListForm",
        "TPianoRollForm",
        "TPythonForm",
        "TFLBridgeValidationForm",
    }
    if gui.GetClassName(hwnd) not in movable:
        raise ValueError("Only FL forms may be moved; controls are excluded")
    if any(type(value) is not int for value in (x, y)):
        raise ValueError("Coordinates must be integers")
    with physical_pixels():
        before = gui.GetWindowRect(hwnd)
        width = before[2] - before[0] if width is None else width
        height = before[3] - before[1] if height is None else height
        if (
            type(width) is not int
            or type(height) is not int
            or not (80 <= width <= 16384 and 40 <= height <= 16384)
        ):
            raise ValueError("Invalid form dimensions")
        if not gui.IsWindowVisible(hwnd):
            raise ValueError("Show the form using FL's native UI API before moving it")
        px, py = x, y
        if gui.GetWindowLong(hwnd, -16) & 0x40000000:
            px, py = gui.ScreenToClient(gui.GetParent(hwnd), (x, y))
        gui.SetWindowPos(hwnd, 0, px, py, width, height, 0x0004 | 0x0010)
        after = gui.GetWindowRect(hwnd)
    return {
        "hwnd": hwnd,
        "before": list(before),
        "after": list(after),
        "verified_position": abs(after[0] - x) <= 1 and abs(after[1] - y) <= 1,
    }


def focus(hwnd: int) -> dict:
    gui, process, pid = require_window(hwnd)
    if not gui.IsWindowVisible(hwnd):
        raise ValueError("Hidden forms must be shown through FL's native API")
    root = gui.GetAncestor(hwnd, 2)
    if gui.IsIconic(root):
        gui.ShowWindow(root, 9)
    try:
        gui.SetForegroundWindow(root)
        gui.BringWindowToTop(hwnd)
    except Exception:
        pass
    time.sleep(0.05)
    if process.GetWindowThreadProcessId(gui.GetForegroundWindow())[1] != pid:
        import win32api

        current_thread = win32api.GetCurrentThreadId()
        foreground_thread = process.GetWindowThreadProcessId(gui.GetForegroundWindow())[0]
        attach = ctypes.windll.user32.AttachThreadInput
        attach.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_bool]
        attach.restype = ctypes.c_bool
        attached = foreground_thread != current_thread and attach(
            current_thread, foreground_thread, True
        )
        try:
            if not attached:
                raise RuntimeError("Windows refused foreground input attachment")
            gui.BringWindowToTop(root)
            gui.SetForegroundWindow(root)
            gui.BringWindowToTop(hwnd)
            time.sleep(0.05)
        except Exception as exc:
            raise RuntimeError("Windows refused focus; no input was sent") from exc
        finally:
            if attached:
                attach(current_thread, foreground_thread, False)
    if process.GetWindowThreadProcessId(gui.GetForegroundWindow())[1] != pid:
        raise RuntimeError("FL did not gain foreground focus; no input was sent")
    return {"success": True, "hwnd": hwnd}


def _point(gui, process, pid, x, y):
    if type(x) is not int or type(y) is not int:
        raise ValueError("Expected integer physical screen coordinates")
    under = gui.WindowFromPoint((x, y))
    if not under or process.GetWindowThreadProcessId(under)[1] != pid:
        raise ValueError("Point is covered by another app or outside FL")


def _unicode_text_events(text):
    from pynput._util.win32 import INPUT, INPUT_union, KEYBDINPUT

    encoded = text.encode("utf-16-le")
    events = []
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset:offset + 2], "little")
        for flags in (KEYBDINPUT.UNICODE, KEYBDINPUT.UNICODE | KEYBDINPUT.KEYUP):
            events.append(INPUT(type=INPUT.KEYBOARD, value=INPUT_union(ki=KEYBDINPUT(wVk=0, wScan=unit, dwFlags=flags))))
    return events


def _literal_text(text):
    from pynput._util.win32 import INPUT, SendInput

    events = _unicode_text_events(text)
    for start in range(0, len(events), 128):
        page = events[start:start + 128]
        inputs = (INPUT * len(page))(*page)
        sent = SendInput(len(page), ctypes.byref(inputs), ctypes.sizeof(INPUT))
        if sent != len(page):
            raise RuntimeError("Text injection was partial; inspect the field before retrying")


def _scroll_with_modifiers(mouse, keyboard, keys, dx, dy, modifiers):
    if not isinstance(modifiers, list) or len(modifiers) > 3:
        raise ValueError("modifiers must contain up to three ctrl/shift/alt names")
    allowed = {"ctrl": keys.ctrl, "shift": keys.shift, "alt": keys.alt}
    if any(name not in allowed for name in modifiers) or len(set(modifiers)) != len(modifiers):
        raise ValueError("Expected unique ctrl/shift/alt modifiers")
    pressed = []
    try:
        for name in modifiers:
            keyboard.press(allowed[name])
            pressed.append(allowed[name])
        if pressed:
            time.sleep(0.05)
        mouse.scroll(dx, dy)
        if pressed:
            time.sleep(0.08)
    finally:
        for key in reversed(pressed):
            keyboard.release(key)


def input_action(hwnd: int, operation: str, **params) -> dict:
    gui, process, pid = require_window(hwnd)
    if not gui.IsWindowVisible(hwnd):
        raise ValueError("Target form is hidden; refresh native/UI state before sending input")
    if process.GetWindowThreadProcessId(gui.GetForegroundWindow())[1] != pid:
        focus(hwnd)
    from pynput.keyboard import Controller as Keyboard, Key
    from pynput.mouse import Button, Controller as Mouse

    with physical_pixels():
        if operation == "keys":
            names = params.get("keys", [])
            if not isinstance(names, list) or not 1 <= len(names) <= 8:
                raise ValueError("keys must be a chord containing 1..8 keys")
            aliases = {
                "ctrl": Key.ctrl,
                "control": Key.ctrl,
                "alt": Key.alt,
                "shift": Key.shift,
                "enter": Key.enter,
                "return": Key.enter,
                "escape": Key.esc,
                "esc": Key.esc,
                "tab": Key.tab,
                "space": Key.space,
                "delete": Key.delete,
                "backspace": Key.backspace,
                "left": Key.left,
                "right": Key.right,
                "up": Key.up,
                "down": Key.down,
                "home": Key.home,
                "end": Key.end,
                "page_up": Key.page_up,
                "page_down": Key.page_down,
            }
            aliases.update({f"f{i}": getattr(Key, f"f{i}") for i in range(1, 21)})
            chord = []
            for name in names:
                if not isinstance(name, str):
                    raise ValueError("Key names must be strings")
                lower = name.lower()
                if lower in aliases:
                    chord.append(aliases[lower])
                elif len(name) == 1:
                    chord.append(name)
                else:
                    raise ValueError("Unknown or unsupported key: " + name)
            keyboard = Keyboard()
            pressed = []
            try:
                for key in chord:
                    keyboard.press(key)
                    pressed.append(key)
                time.sleep(0.05)
            finally:
                for key in reversed(pressed):
                    keyboard.release(key)
        elif operation == "text":
            text = params.get("text")
            if not isinstance(text, str) or len(text) > 8192:
                raise ValueError("Expected up to 8192 literal characters")
            _literal_text(text)
        elif operation in ("click", "drag", "scroll"):
            x, y = params.get("x"), params.get("y")
            _point(gui, process, pid, x, y)
            hit = gui.WindowFromPoint((x, y))
            if gui.GetClassName(hwnd) != "TFruityLoopsMainForm" and not (
                hit == hwnd or gui.IsChild(hwnd, hit)
            ):
                raise ValueError(
                    "Point is covered by a different FL form; refresh the target window"
                )
            mouse = Mouse()
            if not ctypes.windll.user32.SetPhysicalCursorPos(x, y):
                raise RuntimeError("Windows refused the physical cursor position")
            time.sleep(0.12)
            from ctypes import wintypes

            cursor = wintypes.POINT()
            if not ctypes.windll.user32.GetPhysicalCursorPos(ctypes.byref(cursor)) or (
                cursor.x,
                cursor.y,
            ) != (x, y):
                raise RuntimeError("Cursor moved before input; no button event was sent")
            _point(gui, process, pid, x, y)
            hit = gui.WindowFromPoint((x, y))
            if gui.GetClassName(hwnd) != "TFruityLoopsMainForm" and not (
                hit == hwnd or gui.IsChild(hwnd, hit)
            ):
                raise ValueError("Target changed before input; no button event was sent")
            if operation == "click":
                button = {"left": Button.left, "right": Button.right, "middle": Button.middle}.get(
                    params.get("button", "left")
                )
                count = params.get("count", 1)
                if button is None or type(count) is not int or not 1 <= count <= 2:
                    raise ValueError("Invalid mouse button or click count")
                for i in range(count):
                    mouse.press(button)
                    try:
                        time.sleep(0.08)
                    finally:
                        mouse.release(button)
                    if i + 1 < count:
                        time.sleep(0.08)
            elif operation == "scroll":
                dx, dy = params.get("dx", 0), params.get("dy", 0)
                if any(type(v) is not int or abs(v) > 100 for v in (dx, dy)):
                    raise ValueError("Scroll increments must be integers in -100..100")
                _scroll_with_modifiers(mouse, Keyboard(), Key, dx, dy, params.get("modifiers", []))
            else:
                tx, ty = params.get("to_x"), params.get("to_y")
                _point(gui, process, pid, tx, ty)
                try:
                    mouse.press(Button.left)
                    for i in range(1, 21):
                        mouse.position = (
                            round(x + (tx - x) * i / 20),
                            round(y + (ty - y) * i / 20),
                        )
                        time.sleep(0.015)
                finally:
                    mouse.release(Button.left)
        else:
            raise ValueError("operation must be click, drag, scroll, keys or text")
    return {"submitted": True, "operation": operation, "requires_observation": True}


def screenshot(hwnd: int, output: Path) -> dict:
    gui, _, _ = require_window(hwnd)
    if not gui.IsWindowVisible(hwnd):
        raise ValueError("Screenshot target is hidden")
    import win32ui

    with physical_pixels():
        left, top, right, bottom = gui.GetWindowRect(hwnd)
        width, height = right - left, bottom - top
        if not 1 <= width <= 8192 or not 1 <= height <= 8192:
            raise ValueError("Window is minimized or has unsupported dimensions")
        dc = gui.GetWindowDC(hwnd)
        source = win32ui.CreateDCFromHandle(dc)
        memory = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source, width, height)
            memory.SelectObject(bitmap)
            memory.BitBlt((0, 0), (width, height), source, (0, 0), 0x00CC0020)
            data = bitmap.GetBitmapBits(True)
            rows = []
            stride = ((width * 32 + 31) // 32) * 4
            for y in range(height):
                row = data[y * stride : y * stride + width * 4]
                rgb = bytearray(width * 3)
                rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
                rows.append(b"\0" + bytes(rgb))
        finally:
            gui.DeleteObject(bitmap.GetHandle())
            memory.DeleteDC()
            source.DeleteDC()
            gui.ReleaseDC(hwnd, dc)

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    )
    png += chunk(b"IDAT", zlib.compress(b"".join(rows), 3)) + chunk(b"IEND", b"")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)
    return {
        "path": str(output),
        "width": width,
        "height": height,
        "origin": [left, top],
        "hwnd": hwnd,
        "capture_method": "window_dc_bitblt",
        "foreground_activated": False,
    }


def drop_file(hwnd: int, path: str, x: int, y: int, *, prepare_only: bool = False) -> dict:
    """Bound an OLE operation to its own helper, so it cannot hang the MCP server."""
    import json
    import subprocess
    import sys

    _, _, pid = require_window(hwnd)
    payload = json.dumps(dict(pid=pid, hwnd=hwnd, path=path, x=x, y=y, prepare_only=prepare_only))

    def phases(stderr):
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        events = []
        for line in (stderr or "").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and "phase" in event:
                events.append(event)
        return events
    try:
        result = subprocess.run(
            [sys.executable, "-m", "fl_studio_mcp.utils.desktop_drop", payload],
            # The Windows venv launcher can stall on an inherited MCP stdin
            # pipe before Python starts. This helper only accepts argv input.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        trace = phases(exc.stderr)
        return {
            "success": False,
            "submitted": None,
            "error_type": "OutcomeUnknown",
            "error": "OLE helper timed out; inspect FL state before retrying",
            "verified_import": False,
            "requires_native_readback": True,
            "helper_trace": trace,
            "last_phase": trace[-1]["phase"] if trace else "helper_startup_unknown",
            "prepare_only": prepare_only,
        }
    try:
        response = json.loads(result.stdout)
        response["helper_trace"] = phases(result.stderr)
        return response
    except ValueError as exc:
        raise RuntimeError(
            "OLE helper failed without a valid result: " + result.stderr[-1000:]
        ) from exc


def _drop_file_in_process(
    hwnd: int, path: str, x: int, y: int, *, prepare_only: bool = False, trace=None
) -> dict:
    """Use Windows' real shell IDataObject/OLE protocol, not synthetic HDROP pointers."""
    if trace is None:
        def trace(phase, **details):
            pass

    trace("target_validation")
    gui, process, pid = require_window(hwnd)
    if not gui.IsWindowVisible(hwnd):
        raise ValueError("Drop target is hidden")
    file = Path(path).resolve(strict=True)
    if not file.is_file() or file.suffix.lower() not in (
        ".wav",
        ".aif",
        ".aiff",
        ".mp3",
        ".ogg",
        ".flac",
        ".mid",
        ".midi",
        ".fst",
        ".fxp",
        ".fxb",
        ".vstpreset",
    ):
        raise ValueError("Expected an existing audio, MIDI or plugin preset file")
    trace("file_validated")
    import pythoncom
    import winerror
    from win32com.shell import shell, shellcon
    from win32com.server.util import wrap

    class DropSource:
        _com_interfaces_ = [pythoncom.IID_IDropSource]
        _public_methods_ = ["QueryContinueDrag", "GiveFeedback"]

        def __init__(self):
            self.deadline = time.monotonic() + 0.5
            self.pointer_trace = []
            self.first_query = True

        def QueryContinueDrag(self, escape, keys):
            if self.first_query:
                trace("drag_source_callback")
                self.first_query = False
            point = gui.GetCursorPos()
            message_point = ctypes.windll.user32.GetMessagePos()
            self.pointer_trace.append(
                {
                    "cursor": list(point),
                    "message": [message_point & 0xFFFF, (message_point >> 16) & 0xFFFF],
                }
            )
            del self.pointer_trace[:-8]
            if escape or point != (x, y):
                return winerror.DRAGDROP_S_CANCEL
            return 0 if time.monotonic() < self.deadline else winerror.DRAGDROP_S_DROP

        def GiveFeedback(self, effect):
            return winerror.DRAGDROP_S_USEDEFAULTCURSORS

    trace("ole_initialize")
    pythoncom.OleInitialize()
    try:
        trace("shell_file_parse")
        pidl = shell.SHParseDisplayName(str(file), 0)[0]
        trace("shell_folder_parse")
        folder = shell.SHParseDisplayName(str(file.parent), 0)[0]
        trace("shell_data_create")
        data = shell.SHCreateDataObject(folder, [pidl[-1:]], None, pythoncom.IID_IDataObject)
        trace("shell_data_ready")
        if prepare_only:
            return {"success": True, "prepared": True, "submitted": False, "mouse_moved": False}
        if process.GetWindowThreadProcessId(gui.GetForegroundWindow())[1] != pid:
            trace("foreground_focus")
            focus(hwnd)
        source_object = DropSource()
        source = wrap(source_object, pythoncom.IID_IDropSource)
        with physical_pixels():
            trace("drop_target_check")
            _point(gui, process, pid, x, y)
            hit = gui.WindowFromPoint((x, y))
            if gui.GetClassName(hwnd) != "TFruityLoopsMainForm" and not (
                hit == hwnd or gui.IsChild(hwnd, hit)
            ):
                raise ValueError("Drop point is covered by a different FL form")
            if not ctypes.windll.user32.SetPhysicalCursorPos(x, y):
                raise RuntimeError("Windows refused the drop cursor position")
            trace("cursor_positioned")
            time.sleep(0.12)

            trace("drag_loop_enter")
            effect = _run_ole_drop(data, source, shellcon.DROPEFFECT_COPY)
            trace("drag_loop_returned", effect=effect)
        return {
            "submitted": bool(effect),
            "effect": effect,
            "path": str(file),
            "verified_import": False,
            "requires_native_readback": True,
            "target_point": [x, y],
            "pointer_trace": source_object.pointer_trace,
        }
    finally:
        trace("ole_uninitialize")
        ctypes.windll.ole32.OleUninitialize()
        trace("helper_completed")


def _run_ole_drop(data, source, effect):
    """Wake OLE's own thread queue; a background helper receives no global mouse events."""
    import pythoncom

    thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    finished = threading.Event()

    def wake():
        from ctypes import wintypes

        while not finished.wait(0.1):
            point = wintypes.POINT()
            ctypes.windll.user32.GetPhysicalCursorPos(ctypes.byref(point))
            coordinates = (point.x & 0xFFFF) | ((point.y & 0xFFFF) << 16)
            ctypes.windll.user32.PostThreadMessageW(thread_id, 0x0200, 0, coordinates)

    thread = threading.Thread(target=wake, daemon=True)
    thread.start()
    try:
        return pythoncom.DoDragDrop(data, source, effect)
    finally:
        finished.set()
        thread.join(timeout=1)
