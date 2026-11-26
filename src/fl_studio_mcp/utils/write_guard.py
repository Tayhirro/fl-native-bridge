"""Shared write guards for compound native operations.

Clone and swap must apply the same rules: pin the instance/project context
before the first mutation, keep two distinct request IDs per step, and only
release recovery artifacts once every submitted request has left the queue.
The caller injects ``queue_status`` and ``capabilities`` so the MCP module
stays the single patch point for tests and keeps its public tool names.
"""

from __future__ import annotations

from pathlib import Path


def step_outcome(response: dict) -> str:
    """Classify one RPC response without guessing at an ambiguous write."""
    if (response.get("error_type") == "OutcomeUnknown"
            or response.get("state") in ("outcome_unknown", "queued")):
        return "outcome_unknown"
    if not response.get("success"):
        return "rejected_before_write" if not response.get("submitted") else "known_failure"
    if response.get("submitted") and response.get("verified") is False:
        return "partial"
    return "completed"


def step_record(name: str, correlation_id: str, response: dict,
                slot_state: dict | None = None) -> dict:
    """Keep the tool correlation ID and the queue envelope ID separate.

    ``correlation_id`` is the ID the tool put into params and expects echoed
    back (``effect_request_id``/``native_request_id``/``capability_request_id``).
    ``queue_request_id`` is the envelope ID ``FileRPC.send`` created; only that
    ID can be passed to ``fl_get_request_status``. Mixing them is how a timed
    out mutation becomes untraceable.
    """
    record = {
        "step": name,
        "correlation_id": correlation_id,
        "queue_request_id": response.get("queue_request_id"),
        "submitted": bool(response.get("submitted")),
        "outcome": step_outcome(response),
    }
    if slot_state is not None:
        record["slot_state"] = slot_state
    if response.get("error"):
        record["error"] = response["error"]
    return record


def status_request_ids(steps: list[dict]) -> list[str]:
    return [step["queue_request_id"] for step in steps
            if isinstance(step.get("queue_request_id"), str)]


def terminal(steps: list[dict], queue_status) -> bool:
    """True only when every submitted request has left the queue."""
    for step in steps:
        request_id = step.get("queue_request_id")
        if not request_id or not step.get("submitted"):
            continue
        try:
            state = queue_status(request_id).get("state")
        except (OSError, ValueError, TypeError):
            return False
        if state in ("queued", "outcome_unknown") or state is None:
            return False
    return True


def release_when_final(paths: list[Path], steps: list[dict], queue_status) -> list[str]:
    """Delete temp artifacts once no submitted request can still read them."""
    if terminal(steps, queue_status):
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return []
    return [str(path) for path in paths if path.exists()]


def pin_context(capabilities: dict) -> tuple[str, str] | None:
    """Return (context, engine_path) only when the native bridge is usable."""
    native = capabilities.get("native", {})
    if (not capabilities.get("success") or not native.get("available")
            or not isinstance(capabilities.get("context"), str)
            or not isinstance(native.get("engine_path"), str)
            or not native["engine_path"]):
        return None
    return capabilities["context"], native["engine_path"]
