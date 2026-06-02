"""Job-type handler registry.

Register a new job type by decorating a `dict -> dict` function with
`@register("<type>")`. `process_message` dispatches on `job["type"]`.
"""

import socket
from collections.abc import Callable

_HANDLERS: dict[str, Callable[[dict], dict]] = {}


def register(job_type: str):
    """Decorator: register a handler for a job type."""

    def decorator(fn: Callable[[dict], dict]):
        _HANDLERS[job_type] = fn
        return fn

    return decorator


@register("data-transform")
def handle_data_transform(job: dict) -> dict:
    """Example handler: transforms payload.input according to payload.operation."""
    payload = job.get("payload", {})
    text = payload.get("input", "")
    operation = payload.get("operation", "uppercase")

    if operation == "uppercase":
        result = text.upper()
    elif operation == "lowercase":
        result = text.lower()
    elif operation == "reverse":
        result = text[::-1]
    else:
        raise ValueError(f"Unknown operation: {operation}")

    return {"input": text, "operation": operation, "result": result}


@register("ping")
def handle_ping(job: dict) -> dict:
    """Health-check job type — always succeeds."""
    return {"pong": True, "hostname": socket.gethostname()}
