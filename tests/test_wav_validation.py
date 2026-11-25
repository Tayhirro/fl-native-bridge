import math
import struct
import wave

import pytest

from fl_studio_mcp.utils.wav_validation import inspect_wav


def test_pcm_tone_duration_rms_and_analysis_limit(tmp_path):
    path = tmp_path / "tone.wav"
    values = [round(3276 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(48000)]
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        stream.writeframes(struct.pack("<48000h", *values))
    result = inspect_wav(path, expected_duration=1)
    assert result["passed"] and result["duration_seconds"] == 1
    assert result["rms"][0] == pytest.approx(0.1 / math.sqrt(2), abs=0.0001)
    limited = inspect_wav(path, max_frames=100)
    assert not limited["complete"] and not limited["passed"]
    assert not inspect_wav(path, expected_duration=2)["passed"]


def test_truncated_render_is_rejected(tmp_path):
    path = tmp_path / "incomplete.wav"
    path.write_bytes(b"RIFF" + struct.pack("<I", 1000) + b"WAVE")
    with pytest.raises(ValueError, match="incomplete"):
        inspect_wav(path)


def test_float_nan_and_clipping_do_not_pass_as_valid_audio(tmp_path):
    path = tmp_path / "float.wav"
    fmt = struct.pack("<HHIIHH", 3, 1, 48000, 192000, 4, 32)
    samples = struct.pack("<4f", float("nan"), 1.1, 0.1, -0.1)
    chunks = (
        b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(samples))
        + samples
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks)
    result = inspect_wav(path)
    assert result["nonfinite_samples"] == 1 and result["full_scale_samples"] == 1
    assert not result["passed"]


def test_legacy_export_never_deletes_an_existing_output(tmp_path, monkeypatch):
    from fl_studio_mcp.utils import audio

    path = tmp_path / "existing.wav"
    path.write_bytes(b"existing audio must remain")

    def unexpected(*args, **kwargs):
        raise AssertionError("Existing-output rejection must precede all automation")

    monkeypatch.setattr(audio.subprocess, "run", unexpected)
    with pytest.raises(FileExistsError):
        audio.trigger_export_via_gui(str(path))
    assert path.read_bytes() == b"existing audio must remain"
