"""
Per-turn Redis / DB call accounting.

Both counters behind the ``runtime_metrics`` log line used to be
approximations that lied in opposite directions:

* ``db_calls`` was a delta on the **process-global** connection-acquire
  counter (``database_manager.get_connection_acquire_count``). Every
  acquisition made by any other in-flight turn or background task in the same
  worker landed on whichever turn happened to be open, so the number tracked
  *wall-clock duration*, not work. A 134s webchat turn whose LangSmith trace
  contained fewer than ten queries reported ``db_calls=196`` — enough to send
  an RCA hunting a runaway query loop that did not exist.
* ``redis_calls`` was collected by a context hook installed by
  ``ConversationRuntime`` alone, so guarded Redis ops performed by the surface
  *outside* that window were invisible. On webchat the state persist runs
  after the runtime turn closes, so every webchat turn reported
  ``redis_calls=0`` while really doing Redis work.

This module replaces both with one ``contextvars``-scoped counter. Because the
scope travels with the context, work spawned inside a turn (``asyncio`` tasks,
``asyncio.gather`` fan-out, ``asyncio.to_thread`` calls) is attributed to the
turn it belongs to, while concurrent turns each keep their own tally. Scopes
nest: an inner scope chains its Redis hook to the parent, so a surface can
wrap a wider window than the runtime without either one losing ops.

The scope is observability-only. Nothing here may raise into a request path:
a failure to count is always preferable to a failed turn.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from fashion_bot.utils.redis_guard import (
    get_redis_guard_op_hook,
    reset_redis_guard_op_hook,
    set_redis_guard_op_hook,
)

logger = logging.getLogger(__name__)


@dataclass
class TurnMetrics:
    """Mutable per-turn counters, shared by reference across the turn's context.

    Mutating a shared object rather than re-``set``-ing a ContextVar is
    deliberate: a child asyncio Task gets a *copy* of the context, so a
    ``ContextVar.set`` inside it would never reach the parent, but the object
    the copied var points at is the same one. Fan-out inside a turn therefore
    counts toward that turn.
    """

    db_calls: int = 0
    redis_calls: int = 0
    redis_calls_by_op: Dict[str, int] = field(default_factory=dict)

    def record_db_call(self) -> None:
        self.db_calls += 1

    def record_redis_op(self, op_name: str) -> None:
        self.redis_calls += 1
        self.redis_calls_by_op[op_name] = self.redis_calls_by_op.get(op_name, 0) + 1


@dataclass
class TurnMetricsToken:
    """Reset handles for one opened scope. Opaque to callers."""

    metrics_token: Token
    hook_token: Any


_ACTIVE_TURN_METRICS: ContextVar[Optional[TurnMetrics]] = ContextVar(
    "active_turn_metrics",
    default=None,
)

# Real nesting is two deep (a surface scope wrapping the runtime's), so this cap
# is pure headroom. It exists because chaining has a failure mode plain
# replacement does not: when a scope's reset is skipped (see
# ``close_turn_metrics``), its hook stays installed, and the next scope opened in
# that same context would chain onto the orphan. Repeated in one long-lived
# context, that grows a chain every Redis op has to walk. Past the cap we drop
# the parent instead — the links being dropped belong to scopes nobody can still
# read, so no live counter loses an op.
_MAX_HOOK_CHAIN_DEPTH = 4
_HOOK_DEPTH_ATTR = "_turn_metrics_chain_depth"


def get_active_turn_metrics() -> Optional[TurnMetrics]:
    """Return the metrics object for the turn in scope, or None outside one."""
    return _ACTIVE_TURN_METRICS.get()


def record_db_call() -> None:
    """Attribute one DB connection acquisition to the active turn.

    No-op outside a turn (cron jobs, workers, startup) — those acquisitions
    still bump the process-global counter in ``database_manager``.
    """
    metrics = _ACTIVE_TURN_METRICS.get()
    if metrics is not None:
        metrics.record_db_call()


def open_turn_metrics() -> Tuple[TurnMetrics, TurnMetricsToken]:
    """Open a metrics scope; returns the live counters and a reset token.

    Mirrors the ``set_redis_guard_op_hook`` / ``reset_redis_guard_op_hook``
    token pattern so it drops into existing ``try/finally`` blocks without
    re-indenting them.
    """
    metrics = TurnMetrics()
    parent_hook = get_redis_guard_op_hook()
    parent_depth = getattr(parent_hook, _HOOK_DEPTH_ATTR, 0) if parent_hook is not None else 0
    if parent_depth >= _MAX_HOOK_CHAIN_DEPTH:
        parent_hook = None
        parent_depth = 0

    def _hook(op_name: str, ok: bool, err: Optional[Dict[str, Any]]) -> None:
        metrics.record_redis_op(op_name)
        if parent_hook is not None:
            # Chain, so an outer scope (or the runtime's degraded-mode hook)
            # still sees every op performed inside this one.
            try:
                parent_hook(op_name, ok, err)
            except Exception:
                pass

    setattr(_hook, _HOOK_DEPTH_ATTR, parent_depth + 1)
    hook_token = set_redis_guard_op_hook(_hook)
    metrics_token = _ACTIVE_TURN_METRICS.set(metrics)
    return metrics, TurnMetricsToken(metrics_token=metrics_token, hook_token=hook_token)


def close_turn_metrics(token: TurnMetricsToken) -> None:
    """Close a scope opened by ``open_turn_metrics``.

    The ``ValueError`` / ``RuntimeError`` swallow mirrors
    ``reset_redis_guard_op_hook`` and exists for the same reason: when
    ``run_turn_stream`` (an async generator) is aborted by a client
    disconnect mid-LLM-call, its ``finally`` runs from asyncio's task-cleanup
    machinery in a *descendant* Context, and ``ContextVar.reset`` rejects a
    token minted elsewhere. This scope is observability-only, so skipping the
    reset leaks nothing and changes no request semantics — whereas letting the
    exception escape would surface through LangChain's callback chain as a
    bogus ``on_llm_error``.
    """
    try:
        _ACTIVE_TURN_METRICS.reset(token.metrics_token)
    except (ValueError, RuntimeError):
        pass
    reset_redis_guard_op_hook(token.hook_token)


@contextmanager
def turn_metrics_scope() -> Iterator[TurnMetrics]:
    """Context-manager form of ``open_turn_metrics`` / ``close_turn_metrics``."""
    metrics, token = open_turn_metrics()
    try:
        yield metrics
    finally:
        close_turn_metrics(token)


def _resolve_trace_id(fn: Callable, args: tuple, kwargs: dict, arg_name: str) -> str:
    """Best-effort read of the trace id argument. Never raises."""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return str(bound.arguments.get(arg_name) or "unknown")
    except Exception:
        return "unknown"


def track_turn_metrics(
    label: str = "turn_metrics",
    trace_id_arg: str = "trace_id",
) -> Callable:
    """Decorate an async turn handler so its full window is measured and logged.

    Use this on a surface handler that does Redis/DB work *around* the
    ``ConversationRuntime`` call (storing the inbound row, persisting state
    after the reply) — the runtime's own ``runtime_metrics`` line covers only
    the window it owns, and nesting is safe.

    Emits one INFO line on every exit path, including exceptions, so a turn
    that fails still reports what it consumed.
    """

    def _decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def _wrapper(*args: Any, **kwargs: Any):
            started = time.time()
            trace_id = _resolve_trace_id(fn, args, kwargs, trace_id_arg)
            metrics, token = open_turn_metrics()
            try:
                return await fn(*args, **kwargs)
            finally:
                close_turn_metrics(token)
                try:
                    elapsed_ms = int((time.time() - started) * 1000)
                    logger.info(
                        f"[TRACE_ID={trace_id}] 🧮 {label} "
                        f"redis_calls={metrics.redis_calls} "
                        f"db_calls={metrics.db_calls} "
                        f"elapsed_ms={elapsed_ms}"
                    )
                except Exception:
                    pass

        return _wrapper

    return _decorate
