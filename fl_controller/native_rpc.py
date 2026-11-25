"""Single-consumer file RPC, run only from FL controller callbacks."""

import json
import os
import time
from pathlib import Path

VERSION = 2
BUILD = "2026-11-16.1"
GENERATION = str(time.time_ns())
ROOT = (
    Path.home()
    / "Documents/Image-Line/FL Studio/Settings/Hardware/FLStudioMCP/native_rpc"
    / str(os.getpid())
)
_last_poll = 0.0
_last_heartbeat = 0.0
_events = []
_revision = 0
_retry_after = 0.0
_last_error = None


def atomic_json(path, value):
    # FL's embedded host rejects rename/replace/mkdir/unlink. Each response has
    # a unique filename; readers retry incomplete JSON until the writer closes.
    path.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False), encoding="utf-8")


def event(kind, **data):
    global _revision
    _revision += 1
    _events.append(dict(revision=_revision, kind=kind, time=time.time(), **data))
    del _events[:-256]


def events_since(revision):
    return dict(
        generation=GENERATION,
        revision=_revision,
        oldest_revision=_events[0]["revision"] if _events else _revision,
        history_gap=bool(_events and revision < _events[0]["revision"] - 1),
        events=[e for e in _events if e["revision"] > revision],
    )


def poll(dispatch):
    """Do not let filesystem errors create an exception storm in FL's UI callback."""
    global _retry_after, _last_error
    now = time.monotonic()
    if now < _retry_after:
        return
    try:
        _poll(dispatch)
    except Exception as exc:
        _retry_after = now + 2.0
        error = (type(exc).__name__, str(exc))
        if error != _last_error:
            import traceback

            print("FL native RPC temporarily paused:", traceback.format_exc())
            _last_error = error


def _poll(dispatch):
    global _last_poll, _last_heartbeat
    now = time.monotonic()
    if now - _last_poll < 0.025:
        return
    _last_poll = now
    if now - _last_heartbeat > 0.5:
        atomic_json(
            ROOT / "heartbeat.json",
            dict(
                protocol=VERSION,
                controller_build=BUILD,
                pid=os.getpid(),
                time=time.time(),
                revision=_revision,
                generation=GENERATION,
            ),
        )
        _last_heartbeat = now
    pending = ROOT / "pending"
    processed = 0
    # Even pathlib.glob invokes an unsupported audit hook in this FL host.
    # External clients publish an atomic directory index under a process lock.
    index_path = ROOT / "queue.json"
    if not index_path.exists():
        return
    request_ids = json.loads(index_path.read_text(encoding="utf-8"))
    for request_id in request_ids:
        if not isinstance(request_id, str):
            continue
        if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            continue
        path = pending / (request_id + ".json")
        if not path.exists():
            continue
        response_path = ROOT / "responses" / path.name
        inflight = ROOT / "inflight" / path.name
        if response_path.exists() or inflight.exists():
            # A duplicate must never replay a mutation, including after a reload.
            continue
        if processed >= 4:
            break
        processed += 1
        request = {}
        try:
            # One FL callback is the sole consumer. Persist a receipt before
            # execution so reloads never replay an uncertain operation.
            inflight.write_text("{}", encoding="utf-8")
            request = json.loads(path.read_text(encoding="utf-8"))
            if request.get("request_id") != request_id or request.get("protocol") != VERSION:
                raise ValueError("Invalid request envelope")
            if time.time() > request["deadline"]:
                raise TimeoutError("Request expired before execution; no operation was performed")
            if request["action"] == "native.events":
                result = events_since(int(request.get("params", {}).get("since", 0)))
            else:
                result = dispatch(request["action"], request.get("params", {}))
            if not isinstance(result, dict):
                result = {"result": result}
            response = dict(result)
            response["success"] = not bool(result.get("error")) and result.get("success", True)
        except Exception as exc:
            response = {"success": False, "error": str(exc), "error_type": type(exc).__name__}
        response.update(
            request_id=request_id,
            protocol=VERSION,
            completed_at=time.time(),
            action=request.get("action"),
            client_pid=request.get("client_pid"),
        )
        try:
            atomic_json(response_path, response)
        except Exception as exc:
            print("Native RPC response error:", exc)


def stop():
    atomic_json(
        ROOT / "heartbeat.json", dict(protocol=VERSION, pid=os.getpid(), time=0, stopped=True)
    )
