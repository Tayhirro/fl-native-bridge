from types import SimpleNamespace

import pytest

from fl_studio_mcp.utils.desktop import _scroll_with_modifiers


def test_scroll_modifiers_are_released_when_wheel_submission_fails():
    events = []
    keyboard = SimpleNamespace(
        press=lambda k: events.append(("press", k)), release=lambda k: events.append(("release", k))
    )
    keys = SimpleNamespace(ctrl="CTRL", shift="SHIFT", alt="ALT")

    def failure(*args):
        raise RuntimeError("injection failed")

    with pytest.raises(RuntimeError):
        _scroll_with_modifiers(
            SimpleNamespace(scroll=failure), keyboard, keys, 0, 1, ["shift", "alt"]
        )
    assert events == [
        ("press", "SHIFT"),
        ("press", "ALT"),
        ("release", "ALT"),
        ("release", "SHIFT"),
    ]


def test_invalid_scroll_modifier_never_presses_a_key():
    def unexpected(*args):
        raise AssertionError("Invalid modifiers must not inject input")

    keyboard = SimpleNamespace(press=unexpected, release=unexpected)
    keys = SimpleNamespace(ctrl="CTRL", shift="SHIFT", alt="ALT")
    with pytest.raises(ValueError):
        _scroll_with_modifiers(
            SimpleNamespace(scroll=unexpected), keyboard, keys, 0, 1, ["windows"]
        )


def test_literal_text_uses_unicode_packets_for_ascii_cjk_and_surrogates():
    from fl_studio_mcp.utils.desktop import _unicode_text_events

    text = "D:\\mix\\中文🎵.wav"
    encoded = text.encode("utf-16-le")
    events = _unicode_text_events(text)
    units = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]
    assert len(events) == len(units) * 2
    assert all(event.value.ki.wVk == 0 for event in events)
    assert [event.value.ki.wScan for event in events[::2]] == units
    assert all(event.value.ki.dwFlags == 4 for event in events[::2])
    assert all(event.value.ki.dwFlags == 6 for event in events[1::2])
