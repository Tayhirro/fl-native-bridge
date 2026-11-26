"""FLP event-stream parsing shared by the semantic native tools.

Pure byte-level helpers: no controller calls, no RPC, no filesystem. Keeping
them here lets Mixer/Playlist/Sampler workflows share one parser instead of
each growing its own tolerant scanner. The semantic tools re-export these
names so existing imports and test patch points stay stable.
"""

from __future__ import annotations


def flp_read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode an FLP-style little-endian base-128 varint at pos."""
    value = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    raise ValueError("Truncated FLP varint")


def flp_varint(value: int) -> bytes:
    """Encode a non-negative int as an FLP-style base-128 varint."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def flp_event_layout(data: bytes) -> tuple[int, bytes, list[dict]]:
    """Split an FLP/FST image into (chunk_offset, header_bytes, events).

    Each event keeps its raw prefix (id byte + length bytes) and payload, so the
    stream can be rebuilt byte-for-byte except for the event being replaced.
    """
    if len(data) < 22 or data[:4] != b"FLhd":
        raise ValueError("Not an FL Studio image (missing FLhd)")
    header_length = int.from_bytes(data[4:8], "little")
    chunk = 8 + header_length
    if data[chunk:chunk + 4] != b"FLdt":
        raise ValueError("No FLdt data chunk")
    data_length = int.from_bytes(data[chunk + 4:chunk + 8], "little")
    pos, end = chunk + 8, chunk + 8 + data_length
    if end > len(data):
        raise ValueError("Truncated FLdt chunk")
    events = []
    while pos < end:
        start = pos
        event_id = data[pos]
        pos += 1
        if event_id == 0xAC:
            size = 1 + (2 if pos < end and data[pos] == 1 else 4)
        elif event_id < 64:
            size = 1
        elif event_id < 128:
            size = 2
        elif event_id < 192:
            size = 4
        else:
            value = shift = 0
            while True:
                if pos >= end:
                    raise ValueError("Truncated event length")
                byte = data[pos]
                pos += 1
                value |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            size = value
        payload_start = pos
        pos += size
        if pos > end:
            raise ValueError("Event payload overruns the chunk")
        events.append({"id": event_id, "start": start,
                       "prefix": data[start:payload_start],
                       "payload": data[payload_start:pos]})
    if pos != end:
        raise ValueError("Event walk did not land on the chunk end")
    return chunk, data[:chunk + 4], events


def fst_with_plugin_state(skeleton: bytes, state: bytes) -> bytes:
    """Rebuild an FST skeleton with its event-213 payload replaced by `state`."""
    _chunk, header, events = flp_event_layout(skeleton)
    has_213 = any(event["id"] == 213 for event in events)
    body = bytearray()
    replaced = False
    for event in events:
        if event["id"] == 213 and not replaced:
            # Regenerate id + length: the new state may differ in size.
            body.append(213)
            body += flp_varint(len(state))
            body += state
            replaced = True
        else:
            body += event["prefix"]
            body += event["payload"]
    if not has_213:
        body.append(213)
        body += flp_varint(len(state))
        body += state
    return header + len(body).to_bytes(4, "little") + bytes(body)
