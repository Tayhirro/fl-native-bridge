"""Correlated multi-client RPC; never retries or replays an ambiguous mutation."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_selected_pid: int | None = None
EXPECTED_CONTROLLER_BUILD = "2026-11-16.1"


def select_instance(pid: int) -> None:
    global _selected_pid
    _selected_pid = pid


class FileRPC:
    @contextmanager
    def queue_lock(self, name="queue.lock"):
        """Serialize index publication across independent MCP/client processes."""
        if (
            not isinstance(name, str)
            or not name
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for c in name)
        ):
            raise ValueError("Invalid lock name")
        # Several clients can create the lock file at the same moment; on Windows
        # the loser of that race gets a sharing violation from open() itself, so
        # the open is retried before any locking is attempted.
        deadline = time.monotonic() + 5
        while True:
            try:
                handle = (self.root / name).open("a+b")
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Queue index could not be opened")
                time.sleep(0.01)
        with handle:
            if os.name == "nt":
                import msvcrt

                # Seeding the first byte and taking the lock are both contended:
                # a client that loses the race gets a sharing violation from the
                # write, not from the lock, so both live inside one retry loop.
                lock_deadline = time.monotonic() + 5
                while True:
                    try:
                        if os.fstat(handle.fileno()).st_size == 0:
                            handle.write(b"\0")
                            handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= lock_deadline:
                            raise TimeoutError("Queue index is locked by another client")
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def refresh_queue(self) -> None:
        with self.queue_lock():
            request_ids = [p.stem for p in (self.root / "pending").glob("*.json")]
            temporary = self.root / "queue.tmp"
            temporary.write_text(json.dumps(request_ids), encoding="utf-8")
            deadline = time.monotonic() + 2
            while True:
                try:
                    os.replace(temporary, self.root / "queue.json")
                    break
                except PermissionError:
                    # Windows readers may briefly hold a non-delete-shared handle.
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.005)

    @staticmethod
    def default_root() -> Path:
        return (
            Path.home() / "Documents/Image-Line/FL Studio/Settings/Hardware/FLStudioMCP/native_rpc"
        )

    @staticmethod
    def running_pids() -> list[int]:
        if os.name != "nt":
            return []
        import win32gui
        import win32process

        pids = set()
        win32gui.EnumWindows(
            lambda hwnd, _: (
                pids.add(win32process.GetWindowThreadProcessId(hwnd)[1])
                if win32gui.GetClassName(hwnd) == "TFruityLoopsMainForm"
                else None
            ),
            0,
        )
        return sorted(pids)

    @staticmethod
    def prepare(base: Path, pid: int) -> None:
        for name in ("pending", "inflight", "responses"):
            (base / str(pid) / name).mkdir(parents=True, exist_ok=True)

    def __init__(self, root: Path | None = None, pid: int | None = None):
        base = root or (
            Path.home() / "Documents/Image-Line/FL Studio/Settings/Hardware/FLStudioMCP/native_rpc"
        )
        self.base = base
        requested = pid or _selected_pid
        if requested:
            self.prepare(base, requested)
            self.root = base / str(requested)
        elif (base / "heartbeat.json").exists():
            self.root = base
        else:
            instances = self.instances(base)
            self.root = base / str(instances[0]["pid"]) if len(instances) == 1 else base
            if not instances and base == self.default_root():
                pids = self.running_pids()
                if len(pids) == 1:
                    self.root = base / str(pids[0])

    @staticmethod
    def instances(base: Path | None = None) -> list[dict]:
        base = base or (
            Path.home() / "Documents/Image-Line/FL Studio/Settings/Hardware/FLStudioMCP/native_rpc"
        )
        if base == FileRPC.default_root():
            for pid in FileRPC.running_pids():
                FileRPC.prepare(base, pid)
        instances = []
        for path in base.glob("*/heartbeat.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("protocol") == 2 and 0 <= time.time() - value["time"] < 5:
                    instances.append(value)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(instances, key=lambda value: value["pid"])

    def heartbeat(self) -> dict:
        for attempt in range(3):
            try:
                result = json.loads((self.root / "heartbeat.json").read_text(encoding="utf-8"))
                result["healthy"] = (
                    result.get("protocol") == 2 and 0 <= time.time() - result["time"] < 5
                )
                return result
            except (OSError, ValueError, KeyError, TypeError):
                if attempt < 2:
                    time.sleep(0.01)
        return {"healthy": False}

    def status(self, request_id: str) -> dict:
        if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            raise ValueError("Invalid request ID")
        response = self.root / "responses" / (request_id + ".json")
        if response.exists():
            value = json.loads(response.read_text(encoding="utf-8"))
            if value.get("request_id") != request_id:
                raise ValueError("Response correlation mismatch")
            # The external process owns filesystem mutations unsupported by FL.
            for directory in ("pending", "inflight"):
                try:
                    (self.root / directory / (request_id + ".json")).unlink(missing_ok=True)
                except OSError:
                    pass
            return {"state": "completed", "response": value}
        for directory, state in (("inflight", "outcome_unknown"), ("pending", "queued")):
            if (self.root / directory / (request_id + ".json")).exists():
                return {"state": state, "request_id": request_id}
        return {"state": "unknown", "request_id": request_id}

    def send(self, action: str, params: dict[str, Any] | None = None, timeout: float = 10) -> dict:
        if (
            not isinstance(action, str)
            or not action
            or (params is not None and not isinstance(params, dict))
        ):
            raise ValueError("Expected a command action and parameter object")
        if not 0 < timeout <= 300:
            raise ValueError("timeout must be in 0..300 seconds")
        heartbeat = self.heartbeat()
        if not heartbeat["healthy"]:
            return {
                "success": False,
                "error": "Controller queue v2 is not active; reload FLStudioMCP in FL's script output window",
                "error_type": "ControllerUnavailable",
            }
        request_id = uuid.uuid4().hex
        pending = self.root / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        envelope = dict(
            protocol=2,
            client_pid=os.getpid(),
            request_id=request_id,
            action=action,
            params=params or {},
            deadline=time.time() + timeout,
        )
        encoded = json.dumps(envelope, ensure_ascii=True, allow_nan=False)
        if len(encoded) > 2_000_000:
            raise ValueError("Request is larger than 2 MB")
        temporary = pending / (request_id + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, pending / (request_id + ".json"))
        self.refresh_queue()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.status(request_id)
            except (OSError, ValueError):
                time.sleep(0.02)
                continue
            if status["state"] == "completed":
                return status["response"]
            time.sleep(0.02)
        try:
            status = self.status(request_id)
        except (OSError, ValueError):
            status = {"state": "outcome_unknown"}
        if status["state"] == "completed":
            return status["response"]
        return {
            "success": False,
            "request_id": request_id,
            "state": status["state"],
            "error": "Timed out; query this request ID before retrying any mutation",
            "error_type": "OutcomeUnknown",
        }
