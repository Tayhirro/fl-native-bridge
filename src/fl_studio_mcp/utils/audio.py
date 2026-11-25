"""Audio utilities for WAV slicing, base64 encoding, and multimodal API calls."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from ctypes import wintypes
from pathlib import Path
from typing import Any


def _set_clipboard(text: str) -> None:
    escaped = text.replace("'", "''")
    subprocess.run(
        ["powershell", "-Command", f"Set-Clipboard -Value '{escaped}'"],
        capture_output=True,
        timeout=10,
    )


def _get_clipboard() -> str:
    result = subprocess.run(
        ["powershell", "-Command", "Get-Clipboard"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def _find_fl_window() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    matches: list[tuple[int, int, int, int]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0: return True
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        if "FL Studio" in buff.value:
            rect = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                matches.append((rect.left, rect.top, rect.right, rect.bottom))
        return True
    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    if not matches:
        raise RuntimeError("FL Studio window not found")
    return matches[0]


def read_bpm_from_flp(flp_path: str | Path) -> float:
    flp_path = Path(flp_path)
    if not flp_path.exists():
        raise RuntimeError(f"FLP file not found: {flp_path}")
    data = flp_path.read_bytes()
    if data[:4] != b"FLhd":
        raise RuntimeError("Not a valid FL Studio project file")
    header_size = struct.unpack("<I", data[4:8])[0]
    pos = 8 + header_size
    if pos + 8 > len(data) or data[pos:pos+4] != b"FLdt":
        raise RuntimeError("FLdt chunk not found")
    fltd_size = struct.unpack("<I", data[pos+4:pos+8])[0]
    fltd_start = pos + 8
    fltd_data = data[fltd_start:fltd_start+fltd_size]
    idx = 1
    while idx < len(fltd_data) and fltd_data[idx] != 0:
        idx += 1
    idx += 1
    idx += 12
    idx += 6
    for _ in range(2):
        while idx + 1 < len(fltd_data):
            if fltd_data[idx] == 0 and fltd_data[idx+1] == 0:
                idx += 2
                break
            idx += 2
    search_end = min(idx + 200, len(fltd_data))
    while idx + 3 < search_end:
        val = struct.unpack("<f", fltd_data[idx:idx+4])[0]
        if 20.0 <= val <= 400.0:
            return round(val, 1)
        idx += 1
    raise RuntimeError("Could not find BPM in FLP file")


def read_bpm_via_gui() -> float:
    try:
        from pynput.keyboard import Controller as KbCtrl
        from pynput.keyboard import Key
        from pynput.mouse import Button
        from pynput.mouse import Controller as MsCtrl
    except ImportError:
        raise RuntimeError("pynput not installed")
    left, top, right, bottom = _find_fl_window()
    width = right - left
    height = bottom - top
    if height < 120:
        click_y = top + height // 2
    user32 = ctypes.windll.user32
    hwnd_list = []
    def _get_hwnd(h, _):
        length = user32.GetWindowTextLengthW(h)
        if length == 0: return True
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(h, buff, length + 1)
        if "FL Studio" in buff.value: hwnd_list.append(h); return False
        return True
    WNDENUMPROC2 = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC2(_get_hwnd), 0)
    if hwnd_list: user32.SetForegroundWindow(hwnd_list[0])
    time.sleep(0.4)
    mouse = MsCtrl(); keyboard = KbCtrl()
    best_bpm: float | None = None
    y_offsets = [35, 38, 42, 48, 55, 65, 75]
    for y_off in y_offsets:
        click_x = left + int(width * 0.55)
        click_y = top + y_off
        mouse.position = (click_x, click_y)
        time.sleep(0.08)
        mouse.click(Button.left, 3)
        time.sleep(0.12)
        keyboard.press(Key.ctrl); keyboard.press("c"); keyboard.release("c"); keyboard.release(Key.ctrl)
        time.sleep(0.12)
        raw = _get_clipboard()
        if raw:
            match = re.search(r"[\d]+\.?\d*", raw)
            if match:
                val = float(match.group())
                if 20.0 <= val <= 400.0:
                    best_bpm = val
                    break
    if best_bpm is not None:
        return best_bpm
    raise RuntimeError("Could not read BPM from any position.")


def trigger_export_via_gui(output_path: str, timeout: float = 60.0) -> Path:
    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)
    if os.path.exists(output_path):
        raise FileExistsError("Choose a new WAV output path; existing audio is never deleted")
    script = _EXPORT_SCRIPT_TEMPLATE.format(
        output_path=output_path.replace("\\", "\\\\"),
        timeout=timeout,
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(script)
        script_path = f.name
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
            cwd=os.path.dirname(script_path),
        )
        if result.returncode != 0 and "EXPORT_DONE" not in (result.stdout or ""):
            raise RuntimeError(f"Export failed (code {result.returncode}): {result.stderr or result.stdout}")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Export helper timed out; render outcome is unknown. Inspect FL and the output before retrying") from exc
    finally:
        try: os.unlink(script_path)
        except Exception: pass
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return Path(output_path)
    raise RuntimeError(f"File '{output_path}' was not created.")


_EXPORT_SCRIPT_TEMPLATE = r'''import os, sys, time, ctypes, subprocess as sp
from ctypes import wintypes

output_path = r"{output_path}"
timeout = {timeout}
os.makedirs(os.path.dirname(output_path), exist_ok=True)

user32 = ctypes.windll.user32
hwnds = []
def cb(h, _):
    l = user32.GetWindowTextLengthW(h)
    if l == 0: return True
    b = ctypes.create_unicode_buffer(l + 1)
    user32.GetWindowTextW(h, b, l + 1)
    if "FL Studio" in b.value: hwnds.append(h); return False
    return True
WEP = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows(WEP(cb), 0)
if not hwnds: print("FL Studio not found"); sys.exit(1)
fl_hwnd = hwnds[0]
user32.SetForegroundWindow(fl_hwnd)
time.sleep(0.4)

from pynput.keyboard import Controller, Key
kb = Controller()

# Ctrl+R
kb.press(Key.ctrl); kb.press("r"); kb.release("r"); kb.release(Key.ctrl)
time.sleep(0.8)

# Find dialog
dialog = []
def cb2(h, _):
    if user32.IsWindowVisible(h):
        r = wintypes.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        w = r.right - r.left
        h2 = r.bottom - r.top
        if 300 < w < 800 and 200 < h2 < 700:
            l = user32.GetWindowTextLengthW(h)
            if l > 0:
                b = ctypes.create_unicode_buffer(l + 1)
                user32.GetWindowTextW(h, b, l + 1)
                if "\u6e32\u67d3" in b.value:
                    dialog.append((h, r))
                    return False
    return True
WEP2 = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows(WEP2(cb2), 0)
if not dialog: print("Dialog not found"); sys.exit(1)

dlg_h, dlg_r = dialog[0]
print("Dialog: (%d,%d)-(%d,%d)" % (dlg_r.left, dlg_r.top, dlg_r.right, dlg_r.bottom))

# Focus dialog
kernel32 = ctypes.windll.kernel32
my_tid = kernel32.GetCurrentThreadId()
dlg_tid = user32.GetWindowThreadProcessId(dlg_h, None)
if my_tid != dlg_tid: user32.AttachThreadInput(my_tid, dlg_tid, True)
user32.SetForegroundWindow(dlg_h)
time.sleep(0.2)
if my_tid != dlg_tid: user32.AttachThreadInput(my_tid, dlg_tid, False)

from pynput.mouse import Controller as MS, Button
mouse = MS()

# Click Start button at hardcoded position
start_x, start_y = 1565, 1113
print("Clicking Start at (%d,%d)" % (start_x, start_y))
mouse.position = (start_x, start_y)
time.sleep(0.3)
mouse.click(Button.left, 1)
time.sleep(0.3)

# Check if file appeared
if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
    print("Render started")
else:
    print("Click did not trigger render")
    sys.exit(1)

# Wait for render to complete
print("Waiting for render...")
start = time.time()
last_size = 0
while time.time() - start < timeout:
    if os.path.exists(output_path):
        sz = os.path.getsize(output_path)
        if sz == last_size and sz > 0:
            time.sleep(1)
            break
        last_size = sz
    time.sleep(0.5)

time.sleep(0.3)
kb.press(Key.enter); kb.release(Key.enter)
print("EXPORT_DONE:" + output_path)
'''


def slice_wav_single(
    input_path: str | Path,
    start_seconds: float,
    end_seconds: float,
    output_path: str | Path,
) -> Path:
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(input_path), "rb") as wav_in:
        params = wav_in.getparams()
        frame_rate = params.framerate
        start_frame = max(0, int(start_seconds * frame_rate))
        end_frame = min(params.nframes, int(end_seconds * frame_rate))
        if end_frame <= start_frame:
            raise ValueError(f"Invalid slice range: {start_seconds}s-{end_seconds}s")
        wav_in.setpos(start_frame)
        frames = wav_in.readframes(end_frame - start_frame)
    with wave.open(str(output_path), "wb") as wav_out:
        wav_out.setparams(params)
        wav_out.writeframes(frames)
    return output_path


def bars_to_seconds(start_bar: int, end_bar: int, bpm: float, beats_per_bar: int = 4) -> tuple[float, float]:
    seconds_per_beat = 60.0 / bpm
    seconds_per_bar = beats_per_bar * seconds_per_beat
    return ((start_bar - 1) * seconds_per_bar, end_bar * seconds_per_bar)


def wav_to_base64(wav_path: str | Path) -> str:
    data = Path(wav_path).read_bytes()
    return base64.b64encode(data).decode("ascii")


def call_multimodal_api(
    audio_base64: str, prompt: str, api_endpoint: str,
    api_key: str | None = None, model: str = "gpt-4o-audio-preview",
    timeout: float = 120.0,
) -> str:
    if api_key is None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("No API key provided and OPENAI_API_KEY not set")
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "input_audio", "input_audio": {"data": audio_base64, "format": "wav"}},
        ]}],
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(api_endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API error {e.code}: {body_text}") from e
    except Exception as e:
        raise RuntimeError(f"API request failed: {e}") from e
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return json.dumps(result, ensure_ascii=False, indent=2)
