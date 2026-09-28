"""
Trace ID context propagation using ContextVar.

Sets a per-request trace_id that is automatically inherited by all async
tasks spawned within the same context. A logging.Filter reads the ContextVar
and injects `record.trace_id` so the formatter can include it in every line.

Usage at entry points (webhook / websocket):
    from fashion_bot.trace_context import set_trace_id, generate_trace_id
    trace_id = generate_trace_id()
    set_trace_id(trace_id)

All downstream logger.info / logger.debug / … calls will automatically
include [TRACE=<trace_id>] in the formatted output.
"""

import logging
import uuid
from contextvars import ContextVar
from typing import Optional

# ── ContextVar ────────────────────────────────────────────────────────────
trace_id_var: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)


def generate_trace_id() -> str:
    """Generate a short unique trace ID (8 hex chars)."""
    return uuid.uuid4().hex[:8]


def set_trace_id(trace_id: str) -> None:
    """Store trace_id in the current async context."""
    trace_id_var.set(trace_id)


def get_trace_id_from_context() -> str:
    """Return trace_id from context, or '-' if not set."""
    return trace_id_var.get() or "-"


# ── Logging Filter ────────────────────────────────────────────────────────
class TraceIdFilter(logging.Filter):
    """Injects `record.trace_id` from the ContextVar so formatters can use %(trace_id)s."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get() or "-"  # type: ignore[attr-defined]
        return True


def install_trace_filter() -> None:
    """Install TraceIdFilter on root logger AND all its handlers.

    The filter must be on handlers (not just the logger) because Python's
    %-style format interpolation happens inside the handler's Formatter,
    which only sees record attributes set by handler-level filters.
    """
    filt = TraceIdFilter()
    root = logging.getLogger()
    if not any(isinstance(f, TraceIdFilter) for f in root.filters):
        root.addFilter(filt)
    for handler in root.handlers:
        if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
            handler.addFilter(filt)
