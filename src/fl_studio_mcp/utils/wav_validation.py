"""Local, bounded WAV output checks. No network calls or project writes."""

import math
from pathlib import Path
import struct


def inspect_wav(path, max_frames=5_000_000, expected_duration=None):
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() != ".wav" or not path.is_file():
        raise ValueError("Expected an existing WAV file")
    if type(max_frames) is not int or not 1 <= max_frames <= 50_000_000:
        raise ValueError("max_frames must be 1..50000000")
    if expected_duration is not None and (
        type(expected_duration) not in (int, float)
        or not math.isfinite(expected_duration)
        or expected_duration <= 0
    ):
        raise ValueError("expected_duration must be a positive finite number")
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError("Expected RIFF/WAVE")
        end = struct.unpack_from("<I", header, 4)[0] + 8
        if not 12 <= end <= size:
            raise ValueError("WAV is incomplete: RIFF size exceeds file")
        fmt = None
        data = None
        while stream.tell() + 8 <= end:
            kind, length = struct.unpack("<4sI", stream.read(8))
            offset = stream.tell()
            if offset + length > end:
                raise ValueError("WAV is incomplete: chunk exceeds RIFF")
            if kind == b"fmt ":
                fmt = stream.read(min(length, 64))
            elif kind == b"data" and data is None:
                data = offset, length
            stream.seek(offset + length + (length & 1))
        if fmt is None or len(fmt) < 16 or data is None:
            raise ValueError("Missing WAV format or data chunk")
        encoding, channels, rate, _, align, bits = struct.unpack_from("<HHIIHH", fmt)
        if encoding == 0xFFFE:
            if len(fmt) < 40 or fmt[26:40] != bytes.fromhex("000000001000800000aa00389b71"):
                raise ValueError("Unsupported extensible WAV format")
            encoding = struct.unpack_from("<H", fmt, 24)[0]
        if not 1 <= channels <= 64 or not 1 <= rate <= 768000:
            raise ValueError("Invalid WAV channel count or sample rate")
        if (
            (encoding == 1 and bits not in (8, 16, 24, 32))
            or (encoding == 3 and bits not in (32, 64))
            or encoding not in (1, 3)
        ):
            raise ValueError("Only PCM or IEEE float WAV is supported")
        width = bits // 8
        if align != channels * width or data[1] % align:
            raise ValueError("WAV data contains an incomplete frame")
        frames = data[1] // align
        checked = min(frames, max_frames)
        stream.seek(data[0])
        peak = [0.0] * channels
        squares = [0.0] * channels
        sums = [0.0] * channels
        nonfinite = clipped = 0
        processed = 0
        limit = 1 - 1 / (1 << (bits - 1)) if encoding == 1 else 1
        while processed < checked:
            count = min(4096, checked - processed)
            chunk = stream.read(count * align)
            if len(chunk) != count * align:
                raise ValueError("WAV data was truncated during inspection")
            if encoding == 3:
                values = (
                    item[0] for item in struct.iter_unpack("<f" if bits == 32 else "<d", chunk)
                )
            elif bits == 8:
                values = ((value - 128) / 128 for value in chunk)
            elif bits == 24:
                values = (
                    int.from_bytes(chunk[i : i + 3], "little", signed=True) / (1 << 23)
                    for i in range(0, len(chunk), 3)
                )
            else:
                values = (
                    item[0] / (1 << (bits - 1))
                    for item in struct.iter_unpack("<h" if bits == 16 else "<i", chunk)
                )
            for index, value in enumerate(values):
                channel = index % channels
                if not math.isfinite(value):
                    nonfinite += 1
                    continue
                peak[channel] = max(peak[channel], abs(value))
                squares[channel] += value * value
                sums[channel] += value
                clipped += abs(value) >= limit
            processed += count
    duration = frames / rate
    duration_matches = (
        expected_duration is None or abs(duration - expected_duration) <= 1 / rate + 1e-9
    )
    return {
        "path": str(path),
        "sample_rate": rate,
        "channels": channels,
        "bits_per_sample": bits,
        "encoding": "PCM" if encoding == 1 else "IEEE_FLOAT",
        "frames": frames,
        "duration_seconds": duration,
        "analyzed_frames": checked,
        "complete": checked == frames,
        "peak": peak,
        "rms": [math.sqrt(value / checked) if checked else 0 for value in squares],
        "dc_offset": [value / checked if checked else 0 for value in sums],
        "nonfinite_samples": nonfinite,
        "full_scale_samples": clipped,
        "non_silent": any(value > 1e-8 for value in peak),
        "duration_matches": duration_matches,
        "passed": bool(
            frames
            and checked == frames
            and not nonfinite
            and not clipped
            and any(value > 1e-8 for value in peak)
            and duration_matches
        ),
        "coverage": "File structure and sample measurements; not a perceptual mixing-quality judgment",
    }
