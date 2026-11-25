"""Send commands to the FL Studio MCP controller directly.

Bypasses the MCP server: writes mcp_command.json and fires the MIDI trigger
note (127) into the virtual MIDI port, then reads mcp_response.json.

Usage:
    uv run --with mido --with python-rtmidi python send_cmd.py commands.json

commands.json format:
    {"commands": [{"action": "...", "params": {...}}, ...]}
    or a single {"action": ..., "params": ...}
"""

import json
import sys
import time
from pathlib import Path

HW = (
    Path.home()
    / "Documents"
    / "Image-Line"
    / "FL Studio"
    / "Settings"
    / "Hardware"
    / "FLStudioMCP"
)
COMMAND_FILE = HW / "mcp_command.json"
RESPONSE_FILE = HW / "mcp_response.json"


def find_port():
    import mido

    ports = mido.get_output_names()
    for name in ports:
        if "IAC" in name or "loopMIDI" in name or "FL" in name.upper():
            return name
    return ports[0] if ports else None


def send(action: str, params: dict | None = None, timeout: float = 6.0) -> dict:
    import mido

    COMMAND_FILE.write_text(
        json.dumps({"action": action, "params": params or {}}), encoding="utf-8"
    )
    if RESPONSE_FILE.exists():
        RESPONSE_FILE.unlink()

    port_name = find_port()
    if not port_name:
        return {"success": False, "error": "no MIDI output port found"}

    out = mido.open_output(port_name)
    out.send(mido.Message("note_on", note=127, velocity=1))
    time.sleep(0.02)
    out.send(mido.Message("note_off", note=127, velocity=0))
    out.close()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if RESPONSE_FILE.exists():
            try:
                return json.loads(RESPONSE_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        time.sleep(0.05)
    return {"success": False, "error": "timeout waiting for response"}


def main():
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    commands = payload.get("commands", [payload])
    for cmd in commands:
        result = send(cmd["action"], cmd.get("params", {}))
        print(
            json.dumps({"action": cmd["action"], "result": result}, ensure_ascii=False)
        )


if __name__ == "__main__":
    main()
