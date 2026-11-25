"""FL Studio connection management via MIDI.

This module provides the connection interface to FL Studio using MIDI messages
and JSON files for communication. It replaces the previous Flapi-based approach.

The communication flow is:
1. MCP server writes command to JSON file
2. MCP server sends MIDI trigger note
3. FL Studio controller script executes the command
4. FL Studio controller writes response to JSON file
5. MCP server reads response
"""

from __future__ import annotations

from typing import Any

from fl_studio_mcp.utils.midi_connection import (
    get_connection as get_midi_connection,
)
from fl_studio_mcp.utils.midi_connection import (
    reset_connection as reset_midi_connection,
)


class FLConnection:
    """Wrapper for FL Studio connection via MIDI.

    Provides a simple interface for sending commands to FL Studio
    and receiving responses.
    """

    def __init__(self) -> None:
        self._midi = get_midi_connection()

    def ensure_connected(self) -> None:
        """Ensure connection to FL Studio is active. Raises RuntimeError if not."""
        self._midi.ensure_connected()

    @property
    def is_connected(self) -> bool:
        """Check if connected to FL Studio."""
        return self._midi.is_connected

    @property
    def connection_error(self) -> str | None:
        """Get the last connection error, if any."""
        return self._midi.connection_error

    def send_command(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Send a command to FL Studio and wait for response.

        Args:
            action: The command action (e.g., "transport.start", "mixer.setTrackVolume")
            params: Optional parameters for the command
            timeout: Maximum time to wait for response in seconds

        Returns:
            Response dictionary from FL Studio

        Raises:
            RuntimeError: If not connected or command fails
        """
        return self._midi.send_command(action, params, timeout)

    def get_status(self) -> dict[str, Any]:
        """Get connection status information."""
        return self._midi.get_status()


# Global connection instance
_connection: FLConnection | None = None


def _ensure_native_rpc_dirs() -> None:
    """Create the per-pid native RPC directories for running FL processes.

    FL's embedded Python cannot create directories, so the controller's
    file-queue heartbeat and responses stay invisible until the directories
    exist. The isolated launcher creates them for its own instances; a
    manually launched FL needs this external step once per process.
    """
    import os
    import subprocess

    try:
        root = (os.path.expanduser("~")
                / "Documents/Image-Line/FL Studio/Settings/Hardware"
                / "FLStudioMCP/native_rpc")
        output = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq FL64.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5).stdout
        for line in output.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == "fl64.exe" and parts[1].isdigit():
                pid_dir = root / parts[1]
                for sub in ("pending", "responses", "inflight"):
                    (pid_dir / sub).mkdir(parents=True, exist_ok=True)
    except Exception:
        # Directory provisioning is best-effort; the MIDI path still works.
        pass


def get_connection() -> FLConnection:
    """Get the global FL Studio connection instance."""
    global _connection
    if _connection is None:
        _ensure_native_rpc_dirs()
        _connection = FLConnection()
    return _connection


def reset_connection() -> None:
    """Reset the connection state to allow reconnection attempts."""
    global _connection
    reset_midi_connection()
    _connection = None
