"""Dump each channel's actual piano roll content (per-channel export).

For each target channel: select it (command file), force FL to foreground,
fire the Piano roll script shortcut (Ctrl+Alt+Y), read piano_roll_state.json.
"""

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from send_cmd import send  # noqa: E402

STATE = (
    Path.home()
    / "Documents"
    / "Image-Line"
    / "FL Studio"
    / "Settings"
    / "Piano roll scripts"
    / "piano_roll_state.json"
)

CHANNELS = [0, 1, 2, 3, 4, 14, 15, 31, 32, 33, 34, 35, 36, 37, 38, 39]

ROLL_TITLE_MARKS = ("Piano roll", "钢琴卷帘")


def find_fl_window():
    import win32gui

    target = None

    def cb(hwnd, _):
        nonlocal target
        if win32gui.IsWindowVisible(hwnd):
            if "FL Studio" in win32gui.GetWindowText(hwnd):
                target = hwnd

    win32gui.EnumWindows(cb, None)
    return target


def all_window_titles(fl_hwnd):
    import win32gui

    titles = []

    def top_cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            titles.append(win32gui.GetWindowText(hwnd))

    def child_cb(hwnd, _):
        titles.append(win32gui.GetWindowText(hwnd))

    win32gui.EnumWindows(top_cb, None)
    if fl_hwnd:
        win32gui.EnumChildWindows(fl_hwnd, child_cb, None)
    return titles


def find_roll_window(fl_hwnd):
    for t in all_window_titles(fl_hwnd):
        for mark in ROLL_TITLE_MARKS:
            if mark in t:
                return t
    return None


def force_foreground(fl_hwnd):
    import win32api
    import win32gui
    import win32process

    for attempt in range(3):
        try:
            fg = win32gui.GetForegroundWindow()
            fg_thread = win32process.GetWindowThreadProcessId(fg)[0]
            my_thread = win32api.GetCurrentThreadId()
            if fg_thread != my_thread:
                win32process.AttachThreadInput(my_thread, fg_thread, True)
            win32gui.BringWindowToTop(fl_hwnd)
            win32gui.SetForegroundWindow(fl_hwnd)
            if fg_thread != my_thread:
                win32process.AttachThreadInput(my_thread, fg_thread, False)
        except Exception:
            pass
        time.sleep(0.15)
        if win32gui.GetForegroundWindow() == fl_hwnd:
            return True
    return False


def press(key_name):
    from pynput.keyboard import Controller, Key

    k = Controller()
    key = getattr(Key, key_name, key_name)
    k.press(key)
    k.release(key)


def hotkey(ctrl_alt_y=True):
    from pynput.keyboard import Controller, Key

    k = Controller()
    k.press(Key.ctrl)
    k.press(Key.alt)
    k.press("y")
    k.release("y")
    k.release(Key.alt)
    k.release(Key.ctrl)


def main():
    fl = find_fl_window()
    if not fl:
        print("FL Studio main window not found")
        return

    if not force_foreground(fl):
        print("could not force FL to foreground; aborting")
        return

    roll = find_roll_window(fl)
    if not roll:
        press("f7")
        time.sleep(1.0)
        roll = find_roll_window(fl)
        print("pressed F7, roll window now:", roll)

    results = []
    for idx in CHANNELS:
        info = send("channels.getInfo", {"index": idx})
        name = info.get("name", f"ch{idx}")

        send("channels.selectOne", {"index": idx})
        time.sleep(0.4)

        if not force_foreground(fl):
            results.append({"index": idx, "name": name, "error": "focus lost"})
            print(json.dumps(results[-1], ensure_ascii=False))
            continue

        before = STATE.stat().st_mtime if STATE.exists() else 0
        hotkey()
        time.sleep(1.2)
        after = STATE.stat().st_mtime if STATE.exists() else 0

        entry = {"index": idx, "name": name, "exported": after != before}
        if entry["exported"]:
            try:
                st = json.loads(STATE.read_text(encoding="utf-8"))
                notes = st.get("notes", [])
                pitches = [n["midi"] for n in notes]
                durs = [n["duration"] for n in notes]
                entry["note_count"] = len(notes)
                entry["pitch_min"] = min(pitches) if pitches else None
                entry["pitch_max"] = max(pitches) if pitches else None
                entry["common_pitches"] = Counter(pitches).most_common(6)
                entry["avg_dur"] = round(sum(durs) / len(durs), 3) if durs else None
            except Exception as e:
                entry["parse_error"] = str(e)
        results.append(entry)
        print(json.dumps(entry, ensure_ascii=False))

        if idx == CHANNELS[0] and not entry["exported"]:
            print("first channel failed to export; aborting the rest")
            break

    Path(__file__).with_name("roll_dump.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("saved roll_dump.json")


if __name__ == "__main__":
    main()
