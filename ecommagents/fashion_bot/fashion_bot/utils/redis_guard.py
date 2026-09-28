"""
Redis resilience helpers.

Provides:
- Timeout-guarded Redis operation execution
- Simple circuit breaker to avoid repeated slow/failing calls
- Structured error metadata for degraded-mode observability
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Dict, Optional, Tuple

from fashion_bot.env_loader import get_bool, get_int

logger = logging.getLogger(__name__)

_REDIS_GUARD_OP_HOOK: ContextVar[Optional[Callable[[str, bool, Optional[Dict[str, Any]]], None]]] = ContextVar(
    "redis_guard_op_hook",
    default=None,
)


def set_redis_guard_op_hook(
    hook: Optional[Callable[[str, bool, Optional[Dict[str, Any]]], None]]
):
    """Set per-context Redis op hook; returns context token for reset."""
    return _REDIS_GUARD_OP_HOOK.set(hook)


def get_redis_guard_op_hook() -> Optional[Callable[[str, bool, Optional[Dict[str, Any]]], None]]:
    """Return the hook currently in scope, or None.

    Lets a nested scope capture its parent and chain to it, so installing an
    inner hook never hides ops from the outer one.
    """
    return _REDIS_GUARD_OP_HOOK.get()


def reset_redis_guard_op_hook(token) -> None:
    """Reset per-context Redis op hook.

    Both exceptions caught here are benign for this hook — it is
    observability-only (forwards Redis op metadata to the request runtime
    for degraded-mode tracking), and the surrounding context is about to
    be torn down anyway. Skipping the reset doesn't leak resources or
    change request semantics.

    Two cleanup-path failure modes are tolerated:

      1. ``ValueError`` — token created in a different asyncio Context.
         Happens when ``run_turn_stream`` (an async generator) sets the
         hook, the chat stream is aborted by a client disconnect mid-LLM-
         call, and ``aclose()`` runs the ``finally`` from asyncio's task-
         cleanup machinery, which executes in a descendant Context.
         Python's ``ContextVar`` rejects cross-context resets with
         ``ValueError: <Token ...> was created in a different Context``.
         Without this swallow, LangChain's callback chain catches the
         propagating ValueError and surfaces it via ``on_llm_error``,
         polluting ``llm_errors_total`` with a misleading
         ``error_type="ValueError"`` increment.

      2. ``RuntimeError`` — token already consumed. Python's
         ``ContextVar.reset()`` raises ``RuntimeError: <Token used ...>
         has already been used once``. Can happen if a finally block runs
         twice (e.g. a finally raising during cleanup, or a defensive
         double-reset in error handling). Same logic applies: the hook
         is observability-only and the second reset would be a no-op
         anyway.

    Both cases also avoid the periodic asyncio "Task exception was never
    retrieved" log noise that the unswallowed exception would produce.
    """
    try:
        _REDIS_GUARD_OP_HOOK.reset(token)
    except (ValueError, RuntimeError):
        # Token came from a different Context (ValueError) or was already
        # consumed (RuntimeError). Both are benign — see docstring.
        pass


@dataclass
class RedisGuardResult:
    ok: bool
    value: Any
    error: Optional[Dict[str, Any]] = None


class RedisGuard:
    """
    Guards Redis operations with timeout + circuit breaker.
    """

    def __init__(
        self,
        timeout_ms: Optional[int] = None,
        fail_threshold: Optional[int] = None,
        reset_seconds: Optional[int] = None,
    ):
        self.timeout_ms = timeout_ms if timeout_ms is not None else get_int("REDIS_OP_TIMEOUT_MS", 2000)
        self.fail_threshold = fail_threshold if fail_threshold is not None else get_int("REDIS_CIRCUIT_FAIL_THRESHOLD", 5)
        self.reset_seconds = reset_seconds if reset_seconds is not None else get_int("REDIS_CIRCUIT_RESET_SECONDS", 30)
        self.enabled = get_bool("ENABLE_REDIS_DEGRADED_FALLBACKS", True)
        self._lock = Lock()
        self._fail_count = 0
        self._opened_at: Optional[float] = None

    def _breaker_state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if (time.time() - self._opened_at) >= self.reset_seconds:
                self._opened_at = None
                self._fail_count = 0
                return "closed"
            return "open"

    def _record_success(self) -> None:
        with self._lock:
            self._fail_count = 0
            self._opened_at = None

    def _record_failure(self) -> None:
        with self._lock:
            self._fail_count += 1
            if self._fail_count >= self.fail_threshold:
                self._opened_at = time.time()

    def execute(
        self,
        op_name: str,
        fn: Callable[[], Any],
        fallback: Any = None,
    ) -> RedisGuardResult:
        """
        Execute an operation with timeout and circuit breaker.

        Returns fallback on failure; never raises.
        """
        op_hook = _REDIS_GUARD_OP_HOOK.get()

        def _emit(ok: bool, err: Optional[Dict[str, Any]]) -> None:
            if not op_hook:
                return
            try:
                op_hook(op_name, ok, err)
            except Exception:
                pass

        if not self.enabled:
            try:
                result = RedisGuardResult(ok=True, value=fn())
                _emit(True, None)
                return result
            except Exception as exc:
                result = RedisGuardResult(
                    ok=False,
                    value=fallback,
                    error={"reason": "exception", "op": op_name, "latency_ms": 0, "error": str(exc)},
                )
                _emit(False, result.error)
                return result

        if self._breaker_state() == "open":
            err = {
                "reason": "circuit_open",
                "op": op_name,
                "latency_ms": 0,
                "error": "circuit breaker open",
            }
            result = RedisGuardResult(ok=False, value=fallback, error=err)
            _emit(False, err)
            return result

        start = time.time()
        try:
            value = fn()
            latency_ms = int((time.time() - start) * 1000)
            if latency_ms > self.timeout_ms:
                raise TimeoutError(f"operation exceeded {self.timeout_ms}ms")
            self._record_success()
            result = RedisGuardResult(ok=True, value=value)
            _emit(True, None)
            return result
        except TimeoutError as exc:
            self._record_failure()
            latency_ms = int((time.time() - start) * 1000)
            err = {
                "reason": "timeout",
                "op": op_name,
                "latency_ms": latency_ms,
                "error": str(exc),
            }
            logger.warning(f"[REDIS_GUARD] timeout op={op_name} latency_ms={latency_ms}")
            result = RedisGuardResult(ok=False, value=fallback, error=err)
            _emit(False, err)
            return result
        except Exception as exc:
            self._record_failure()
            latency_ms = int((time.time() - start) * 1000)
            err = {
                "reason": "exception",
                "op": op_name,
                "latency_ms": latency_ms,
                "error": str(exc),
            }
            logger.warning(f"[REDIS_GUARD] exception op={op_name} latency_ms={latency_ms} err={exc}")
            result = RedisGuardResult(ok=False, value=fallback, error=err)
            _emit(False, err)
            return result

    async def _run_with_timeout(self, awaitable: Any) -> Any:
        timeout_seconds = max(0.001, self.timeout_ms / 1000.0)
        if hasattr(asyncio, "timeout"):
            async with asyncio.timeout(timeout_seconds):
                return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)

    async def execute_async(
        self,
        op_name: str,
        fn: Callable[[], Any],
        fallback: Any = None,
    ) -> RedisGuardResult:
        """
        Async variant of execute().

        `fn` may return either an awaitable or an immediate value.
        """
        op_hook = _REDIS_GUARD_OP_HOOK.get()

        def _emit(ok: bool, err: Optional[Dict[str, Any]]) -> None:
            if not op_hook:
                return
            try:
                op_hook(op_name, ok, err)
            except Exception:
                pass

        async def _call() -> Any:
            value = fn()
            if asyncio.iscoroutine(value) or hasattr(value, "__await__"):
                return await value
            return value

        if not self.enabled:
            try:
                result = RedisGuardResult(ok=True, value=await _call())
                _emit(True, None)
                return result
            except Exception as exc:
                result = RedisGuardResult(
                    ok=False,
                    value=fallback,
                    error={"reason": "exception", "op": op_name, "latency_ms": 0, "error": str(exc)},
                )
                _emit(False, result.error)
                return result

        if self._breaker_state() == "open":
            err = {
                "reason": "circuit_open",
                "op": op_name,
                "latency_ms": 0,
                "error": "circuit breaker open",
            }
            result = RedisGuardResult(ok=False, value=fallback, error=err)
            _emit(False, err)
            return result

        start = time.time()
        try:
            value = await self._run_with_timeout(_call())
            self._record_success()
            result = RedisGuardResult(ok=True, value=value)
            _emit(True, None)
            return result
        except (TimeoutError, asyncio.TimeoutError) as exc:
            self._record_failure()
            latency_ms = int((time.time() - start) * 1000)
            err = {
                "reason": "timeout",
                "op": op_name,
                "latency_ms": latency_ms,
                "error": str(exc) or f"operation exceeded {self.timeout_ms}ms",
            }
            logger.warning(f"[REDIS_GUARD] timeout op={op_name} latency_ms={latency_ms}")
            result = RedisGuardResult(ok=False, value=fallback, error=err)
            _emit(False, err)
            return result
        except Exception as exc:
            self._record_failure()
            latency_ms = int((time.time() - start) * 1000)
            err = {
                "reason": "exception",
                "op": op_name,
                "latency_ms": latency_ms,
                "error": str(exc),
            }
            logger.warning(f"[REDIS_GUARD] exception op={op_name} latency_ms={latency_ms} err={exc}")
            result = RedisGuardResult(ok=False, value=fallback, error=err)
            _emit(False, err)
            return result

    def health(self) -> Dict[str, Any]:
        return {
            "state": self._breaker_state(),
            "fail_count": self._fail_count,
            "timeout_ms": self.timeout_ms,
            "fail_threshold": self.fail_threshold,
            "reset_seconds": self.reset_seconds,
        }


def build_degraded_flags(existing: Optional[Dict[str, Any]], component: str, error: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Merge degraded-mode flags in a state-safe way.
    """
    flags = dict(existing or {})
    flags["degraded_mode"] = True
    degraded_components = list(flags.get("degraded_components") or [])
    if component not in degraded_components:
        degraded_components.append(component)
    flags["degraded_components"] = degraded_components
    flags["last_redis_error_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if error:
        flags["last_redis_error"] = error
    return flags
