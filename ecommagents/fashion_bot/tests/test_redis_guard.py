import asyncio
import contextvars
import time

import pytest

from fashion_bot.utils.redis_guard import (
    RedisGuard,
    _REDIS_GUARD_OP_HOOK,
    reset_redis_guard_op_hook,
    set_redis_guard_op_hook,
)


def test_redis_guard_timeout_returns_fallback():
    guard = RedisGuard(timeout_ms=20, fail_threshold=3, reset_seconds=1)
    result = guard.execute("slow_op", lambda: time.sleep(0.2), fallback="fallback")
    assert result.ok is False
    assert result.value == "fallback"
    assert result.error is not None
    assert result.error.get("reason") == "timeout"


def test_redis_guard_opens_circuit_after_failures():
    guard = RedisGuard(timeout_ms=10, fail_threshold=2, reset_seconds=1)
    _ = guard.execute("fail1", lambda: (_ for _ in ()).throw(RuntimeError("x")), fallback=None)
    _ = guard.execute("fail2", lambda: (_ for _ in ()).throw(RuntimeError("y")), fallback=None)
    result = guard.execute("blocked", lambda: "ok", fallback="f")
    assert result.ok is False
    assert result.value == "f"
    assert result.error is not None
    assert result.error.get("reason") == "circuit_open"


def test_redis_guard_half_open_recovers_after_reset():
    guard = RedisGuard(timeout_ms=20, fail_threshold=1, reset_seconds=1)
    _ = guard.execute("fail", lambda: (_ for _ in ()).throw(RuntimeError("boom")), fallback=None)
    time.sleep(1.1)
    result = guard.execute("recover", lambda: "ok", fallback=None)
    assert result.ok is True
    assert result.value == "ok"
    assert guard.health()["state"] == "closed"


@pytest.mark.asyncio
async def test_redis_guard_async_timeout_returns_fallback():
    guard = RedisGuard(timeout_ms=20, fail_threshold=3, reset_seconds=1)

    async def _slow():
        await asyncio.sleep(0.2)
        return "late"

    result = await guard.execute_async("slow_async_op", _slow, fallback="fallback")

    assert result.ok is False
    assert result.value == "fallback"
    assert result.error is not None
    assert result.error.get("reason") == "timeout"


# ─── reset_redis_guard_op_hook: cross-context safety ──────────────────────
#
# The hook is set in run_turn_stream (an async generator) and reset in its
# finally block. When a chat stream is aborted by client disconnect, the
# generator's aclose() runs in a different asyncio Context than the one
# where set() was called, and Python's ContextVar.reset(token) raises
# ValueError("<Token ...> was created in a different Context"). LangChain's
# callback chain catches that ValueError as if it were an LLM-call error
# and increments llm_errors_total{error_type="ValueError"}, polluting the
# dashboard. The reset is observability-only, so swallowing the cross-
# context ValueError is safe — see docstring on reset_redis_guard_op_hook.
#
# These tests pin that behaviour down.

def _noop_hook(op, ok, meta):  # signature matches the ContextVar declaration
    return None


def test_reset_redis_guard_op_hook_happy_path_clears_the_hook():
    """Sanity check: in the normal same-context case, reset still works."""
    assert _REDIS_GUARD_OP_HOOK.get() is None
    token = set_redis_guard_op_hook(_noop_hook)
    assert _REDIS_GUARD_OP_HOOK.get() is _noop_hook
    reset_redis_guard_op_hook(token)
    assert _REDIS_GUARD_OP_HOOK.get() is None


def test_reset_with_token_from_different_context_does_not_raise():
    """
    The exact production scenario: a token is created inside a child
    Context (simulating run_turn_stream's body) and reset is attempted
    from the parent Context (simulating asyncio task-cleanup running
    aclose() in a different Context).

    Without the try/except in reset_redis_guard_op_hook, Python would
    raise ValueError here and the LangChain callback chain would surface
    that as an llm_errors_total{error_type=ValueError} increment.
    """
    captured = {}

    def _set_in_child_context():
        captured["token"] = set_redis_guard_op_hook(_noop_hook)

    # contextvars.copy_context().run() executes in a fresh child Context.
    # The token captured below is bound to that child Context.
    contextvars.copy_context().run(_set_in_child_context)

    # Calling reset() from the parent Context with a child-Context token
    # is exactly the cross-context cleanup pattern from the bug. Must not
    # raise.
    reset_redis_guard_op_hook(captured["token"])


def test_reset_called_twice_on_same_token_is_tolerated():
    """
    Defensive: a finally block may run twice in some failure modes
    (e.g. a finally raising during cleanup), and the second reset would
    otherwise raise ValueError because the token has already been
    consumed. Same swallow path catches it.
    """
    token = set_redis_guard_op_hook(_noop_hook)
    reset_redis_guard_op_hook(token)
    # Second call must not raise even though the token is already spent.
    reset_redis_guard_op_hook(token)
    assert _REDIS_GUARD_OP_HOOK.get() is None


def test_reset_preserves_hook_value_when_cross_context_reset_skipped():
    """
    When reset is skipped (because the token is from a different
    Context), the hook value in the *current* Context must remain
    untouched — we don't want a benign cross-context cleanup to also
    silently clear a legitimately-set hook in the parent Context.
    """
    # Parent-context hook (different from _noop_hook so identity check
    # distinguishes them)
    def _parent_hook(op, ok, meta):
        return "parent"

    parent_token = set_redis_guard_op_hook(_parent_hook)
    try:
        # Child-context creates its own token
        child_captured = {}

        def _child():
            child_captured["token"] = set_redis_guard_op_hook(_noop_hook)

        contextvars.copy_context().run(_child)

        # Resetting the child token from parent context — swallowed
        # ValueError must NOT mutate the parent's hook value
        reset_redis_guard_op_hook(child_captured["token"])
        assert _REDIS_GUARD_OP_HOOK.get() is _parent_hook
    finally:
        reset_redis_guard_op_hook(parent_token)
