"""
Runtime flow guard helpers.

Tracks whether state writes are executing inside ConversationRuntime orchestration.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, Optional

_RUNTIME_STATE_WRITE_CONTEXT: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "runtime_state_write_context",
    default=None,
)


def activate_runtime_state_write_guard(
    *,
    source: str,
    trace_id: str,
    channel: str,
    client_id: str,
    user_id: str,
):
    """Mark runtime-orchestrated state-write flow as active for this context."""
    payload: Dict[str, Any] = {
        "source": source,
        "trace_id": trace_id,
        "channel": channel,
        "client_id": client_id,
        "user_id": user_id,
    }
    return _RUNTIME_STATE_WRITE_CONTEXT.set(payload)


def deactivate_runtime_state_write_guard(token) -> None:
    """Reset runtime-orchestrated state-write marker for this context."""
    try:
        _RUNTIME_STATE_WRITE_CONTEXT.reset(token)
    except Exception:
        pass


def get_runtime_state_write_context() -> Optional[Dict[str, Any]]:
    """Return active runtime-orchestrated state-write context, if any."""
    return _RUNTIME_STATE_WRITE_CONTEXT.get()

