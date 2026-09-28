"""Tests for per-turn Redis/DB call accounting and the LLM stream stall watchdog.

Covers the two metric defects that made a provider-side stream stall look like a
runaway query loop: `db_calls` measuring elapsed time via a process-global
counter, and `redis_calls` missing every op performed outside the runtime's own
window.
"""

import asyncio

import pytest

from fashion_bot.utils.redis_guard import RedisGuard, get_redis_guard_op_hook
from fashion_bot.utils.turn_metrics import (
    close_turn_metrics,
    get_active_turn_metrics,
    open_turn_metrics,
    record_db_call,
    track_turn_metrics,
    turn_metrics_scope,
)


# ─── DB call attribution ─────────────────────────────────────────────────────

def test_db_calls_counted_only_inside_a_scope():
    record_db_call()  # outside any turn — must not raise, must not be attributed
    assert get_active_turn_metrics() is None

    with turn_metrics_scope() as metrics:
        record_db_call()
        record_db_call()
        assert metrics.db_calls == 2

    assert get_active_turn_metrics() is None


def test_scopes_are_isolated_between_concurrent_turns():
    """The old global-delta counter attributed every concurrent acquisition to
    whichever turn happened to be open — the bug that reported db_calls=196 for
    a turn whose trace held fewer than ten queries."""

    async def _turn(calls: int, hold_s: float) -> int:
        with turn_metrics_scope() as metrics:
            for _ in range(calls):
                record_db_call()
                await asyncio.sleep(hold_s)
            return metrics.db_calls

    async def _run():
        # The slow turn stays open across all of the fast turn's work.
        return await asyncio.gather(_turn(2, 0.05), _turn(6, 0.001))

    slow, fast = asyncio.run(_run())
    assert slow == 2
    assert fast == 6


def test_child_tasks_are_attributed_to_the_parent_turn():
    """asyncio.gather fan-out inside a turn is the turn's own work."""

    async def _leaf():
        record_db_call()

    async def _run():
        with turn_metrics_scope() as metrics:
            await asyncio.gather(*(_leaf() for _ in range(5)))
            return metrics.db_calls

    assert asyncio.run(_run()) == 5


# ─── Redis op attribution ────────────────────────────────────────────────────

def test_redis_guard_ops_are_counted_in_the_active_scope():
    guard = RedisGuard(timeout_ms=1000, fail_threshold=5, reset_seconds=1)
    with turn_metrics_scope() as metrics:
        guard.execute("get_state", lambda: "ok")
        guard.execute("set_state", lambda: "ok")
        guard.execute("set_state", lambda: "ok")

    assert metrics.redis_calls == 3
    assert metrics.redis_calls_by_op == {"get_state": 1, "set_state": 2}


def test_failed_redis_ops_still_count():
    def _boom():
        raise RuntimeError("redis down")

    guard = RedisGuard(timeout_ms=1000, fail_threshold=5, reset_seconds=1)
    with turn_metrics_scope() as metrics:
        result = guard.execute("set_state", _boom, fallback="fallback")

    assert result.ok is False
    assert result.value == "fallback"
    assert metrics.redis_calls == 1


def test_nested_scopes_chain_so_the_outer_scope_loses_nothing():
    """A surface may wrap a wider window than ConversationRuntime; installing the
    inner hook must not hide the inner ops from the outer scope."""
    guard = RedisGuard(timeout_ms=1000, fail_threshold=5, reset_seconds=1)

    with turn_metrics_scope() as outer:
        guard.execute("outer_before", lambda: "ok")
        with turn_metrics_scope() as inner:
            guard.execute("inner_op", lambda: "ok")
        guard.execute("outer_after", lambda: "ok")

    assert inner.redis_calls == 1
    assert inner.redis_calls_by_op == {"inner_op": 1}
    assert outer.redis_calls == 3
    assert outer.redis_calls_by_op == {"outer_before": 1, "inner_op": 1, "outer_after": 1}


def test_hook_chain_is_bounded_when_resets_keep_being_skipped():
    """Worst case for chaining: a long-lived context where every scope's reset is
    skipped (cross-context teardown), so each new scope would chain onto an
    orphaned hook. Without a cap the chain — and the per-op cost of walking it —
    grows for the life of the connection."""
    from fashion_bot.utils import turn_metrics as tm

    def _chain_depth():
        hook = get_redis_guard_op_hook()
        return getattr(hook, tm._HOOK_DEPTH_ATTR, 0) if hook is not None else 0

    outer_token = None
    try:
        for _ in range(30):
            _metrics, token = open_turn_metrics()
            outer_token = outer_token or token
            # Deliberately never closed — simulates the skipped reset.
        assert _chain_depth() <= tm._MAX_HOOK_CHAIN_DEPTH
    finally:
        if outer_token is not None:
            close_turn_metrics(outer_token)


def test_normal_nesting_still_chains_below_the_cap():
    guard = RedisGuard(timeout_ms=1000, fail_threshold=5, reset_seconds=1)

    # Two levels is what production actually does: a surface scope wrapping the
    # runtime's. Both must still see the op.
    with turn_metrics_scope() as surface:
        with turn_metrics_scope() as runtime:
            guard.execute("set_state", lambda: "ok")

    assert runtime.redis_calls == 1
    assert surface.redis_calls == 1


def test_closing_a_scope_restores_the_previous_hook():
    outer_metrics, outer_token = open_turn_metrics()
    outer_hook = get_redis_guard_op_hook()

    _inner_metrics, inner_token = open_turn_metrics()
    assert get_redis_guard_op_hook() is not outer_hook

    close_turn_metrics(inner_token)
    assert get_redis_guard_op_hook() is outer_hook
    assert get_active_turn_metrics() is outer_metrics

    close_turn_metrics(outer_token)
    assert get_active_turn_metrics() is None


def test_close_tolerates_a_token_minted_in_another_context():
    """run_turn_stream is an async generator: when a client disconnects mid-LLM
    call its finally runs from asyncio's cleanup machinery, in a descendant
    Context. ContextVar.reset rejects that token, and the exception must not
    escape into LangChain's callback chain."""
    captured = {}

    async def _open():
        captured["token"] = open_turn_metrics()[1]

    asyncio.run(_open())
    close_turn_metrics(captured["token"])  # must not raise


def test_close_twice_is_tolerated():
    _metrics, token = open_turn_metrics()
    close_turn_metrics(token)
    close_turn_metrics(token)  # must not raise


# ─── Decorator ───────────────────────────────────────────────────────────────

def test_track_turn_metrics_logs_totals_and_returns_the_wrapped_value(caplog):
    @track_turn_metrics()
    async def _handler(message: str, trace_id: str) -> str:
        record_db_call()
        record_db_call()
        return f"reply:{message}"

    with caplog.at_level("INFO"):
        assert asyncio.run(_handler("hi", trace_id="abc1234")) == "reply:hi"

    line = next(r.message for r in caplog.records if "turn_metrics" in r.message)
    assert "[TRACE_ID=abc1234]" in line
    assert "db_calls=2" in line
    assert "redis_calls=0" in line


def test_track_turn_metrics_logs_and_reraises_on_failure(caplog):
    @track_turn_metrics()
    async def _handler(trace_id: str) -> str:
        record_db_call()
        raise ValueError("turn blew up")

    with caplog.at_level("INFO"):
        with pytest.raises(ValueError, match="turn blew up"):
            asyncio.run(_handler(trace_id="deadbeef"))

    line = next(r.message for r in caplog.records if "turn_metrics" in r.message)
    assert "[TRACE_ID=deadbeef]" in line
    assert "db_calls=1" in line


def test_track_turn_metrics_resolves_a_positional_trace_id(caplog):
    """The webchat handlers are all called positionally."""

    @track_turn_metrics()
    async def _handler(websocket, message: str, session: dict, trace_id: str) -> None:
        return None

    with caplog.at_level("INFO"):
        asyncio.run(_handler(object(), "hi", {}, "c7954c01"))

    line = next(r.message for r in caplog.records if "turn_metrics" in r.message)
    assert "[TRACE_ID=c7954c01]" in line


def test_track_turn_metrics_survives_an_unresolvable_trace_id(caplog):
    @track_turn_metrics()
    async def _handler(**kwargs) -> str:
        return "ok"

    with caplog.at_level("INFO"):
        assert asyncio.run(_handler(something="else")) == "ok"

    line = next(r.message for r in caplog.records if "turn_metrics" in r.message)
    assert "[TRACE_ID=unknown]" in line


def test_track_turn_metrics_preserves_function_identity():
    @track_turn_metrics()
    async def _handler(trace_id: str) -> None:
        """Docstring kept."""

    assert _handler.__name__ == "_handler"
    assert _handler.__doc__ == "Docstring kept."


# ─── End-to-end through ConversationRuntime ──────────────────────────────────

def test_runtime_charges_each_turn_only_for_its_own_db_work():
    """Regression for the metric that made trace c7954c01 look like a runaway
    query loop: a 134s turn holding a stalled LLM stream reported db_calls=196
    because the counter was a delta on a process-global value, so it absorbed
    everything every other turn in the worker did while it waited."""
    db = pytest.importorskip("fashion_bot.database_manager")
    presets = pytest.importorskip("fashion_bot.core.runtime_presets")
    runtime_mod = pytest.importorskip("fashion_bot.core.conversation_runtime")

    emitted = []
    runtime = presets.build_passthrough_runtime(
        log_fn=lambda trace_id, message, level="info", *a, **k: emitted.append((trace_id, message))
    )

    async def _stalled_turn(_ctx):
        for _ in range(3):
            db._record_connection_acquire()
        await asyncio.sleep(0.3)  # stands in for the stalled stream
        return runtime_mod.RuntimeResult(handled=True, reply_text="stalled")

    async def _busy_turn(_ctx):
        for _ in range(40):
            db._record_connection_acquire()
            await asyncio.sleep(0.001)
        return runtime_mod.RuntimeResult(handled=True, reply_text="busy")

    async def _run():
        before = db.get_connection_acquire_count()
        stalled, busy = await asyncio.gather(
            runtime.run_turn(
                channel="web", client_id="c1", user_id="u1",
                inbound_payload={}, execute_fn=_stalled_turn, trace_id="stalled1",
            ),
            runtime.run_turn(
                channel="web", client_id="c1", user_id="u2",
                inbound_payload={}, execute_fn=_busy_turn, trace_id="busy0001",
            ),
        )
        return stalled, busy, db.get_connection_acquire_count() - before

    stalled, busy, global_delta = asyncio.run(_run())

    assert stalled.db_calls_total == 3
    assert busy.db_calls_total == 40
    # The old implementation would have reported the whole window for both.
    assert global_delta >= 43
    assert stalled.db_calls_total < global_delta

    metrics_lines = [msg for _tid, msg in emitted if "runtime_metrics" in msg]
    assert any("db_calls=3" in line for line in metrics_lines)
    assert any("db_calls=40" in line for line in metrics_lines)


# ─── LLM stream stall watchdog ───────────────────────────────────────────────

class _FakeChatModel:
    """Stands in for a langchain-openai version that accepts the kwarg."""

    model_fields = {"model": None, "temperature": None, "stream_chunk_timeout": None}


class _LegacyChatModel:
    """Stands in for a pinned-range resolution that predates the kwarg."""

    model_fields = {"model": None, "temperature": None}


@pytest.fixture
def llm_factory(monkeypatch):
    """The factory module with its one-shot 'unsupported' log latch reset.

    ``LLM_STREAM_CHUNK_TIMEOUT_S`` is read through ``env_loader.get_float``,
    which serves a settings snapshot taken at bootstrap rather than live
    ``os.environ`` — so these tests patch the accessor, not the environment.
    """
    mod = pytest.importorskip("fashion_bot.core.llm_factory")
    monkeypatch.setattr(mod, "_stream_chunk_timeout_unsupported_logged", False)
    return mod


def _set_configured_timeout(monkeypatch, mod, value):
    monkeypatch.setattr(mod, "get_float", lambda key, default: value)


def test_watchdog_default_is_applied(llm_factory):
    kwargs = llm_factory._stream_chunk_timeout_kwargs(_FakeChatModel, {})

    assert kwargs == {"stream_chunk_timeout": llm_factory.DEFAULT_STREAM_CHUNK_TIMEOUT_S}
    # Well under the library's 120s default, which is what let a stalled stream
    # burn a whole turn before aborting.
    assert kwargs["stream_chunk_timeout"] < 120


def test_watchdog_respects_configured_override(llm_factory, monkeypatch):
    _set_configured_timeout(monkeypatch, llm_factory, 35.0)

    assert llm_factory._stream_chunk_timeout_kwargs(_FakeChatModel, {}) == {
        "stream_chunk_timeout": 35.0
    }


@pytest.mark.parametrize("disabled", [0.0, -1.0])
def test_watchdog_can_be_disabled(llm_factory, monkeypatch, disabled):
    _set_configured_timeout(monkeypatch, llm_factory, disabled)

    assert llm_factory._stream_chunk_timeout_kwargs(_FakeChatModel, {}) == {}


def test_explicit_per_client_value_wins(llm_factory):
    # Returning {} leaves the caller's own value in additional_params untouched,
    # so the constructor never receives the kwarg twice.
    assert llm_factory._stream_chunk_timeout_kwargs(_FakeChatModel, {"stream_chunk_timeout": 5}) == {}


def test_older_langchain_openai_is_left_alone(llm_factory):
    assert llm_factory._stream_chunk_timeout_kwargs(_LegacyChatModel, {}) == {}


def test_unknown_model_class_is_left_alone(llm_factory):
    class _NoFields:
        pass

    assert llm_factory._stream_chunk_timeout_kwargs(_NoFields, {}) == {}
