from __future__ import annotations

import datetime
import inspect
import json
from typing import Callable

from fashion_bot.core.conversation_runtime import ConversationRuntime
from fashion_bot.env_loader import get_env


async def _await_maybe(value):
    if inspect.isawaitable(value):
        return await value
    return value


def build_passthrough_runtime(log_fn: Callable[..., None]) -> ConversationRuntime:
    """
    Runtime preset for channels that are not yet using Redis single-flight/merge queue.
    Provides a centralized run_turn contract with no behavior change.
    """
    def _no_op(*_args, **_kwargs):
        return None

    def _always_lock(*_args, **_kwargs):
        return True, False

    def _no_queue(*_args, **_kwargs):
        return False

    def _empty_list(*_args, **_kwargs):
        return []

    def _empty_merge(*_args, **_kwargs):
        return {}, 0, 0

    def _summary_disabled(*_args, **_kwargs):
        return False, "disabled"

    return ConversationRuntime(
        log_fn=log_fn,
        mark_degraded_state_fn=_no_op,
        try_acquire_lock_fn=_always_lock,
        enqueue_pending_fn=_no_queue,
        release_lock_fn=_no_op,
        drain_pending_fn=_empty_list,
        build_merged_payload_fn=_empty_merge,
        should_trigger_summary_fn=_summary_disabled,
        enqueue_summary_job_fn=_no_op,
        redispatch_fn=_no_op,
    )


def build_single_flight_failopen_runtime(
    *,
    log_fn: Callable[..., None],
    get_redis_client_fn: Callable[[], object],
    redis_guard,
    lock_ttl_seconds: int | None = None,
) -> ConversationRuntime:
    """
    Runtime preset for interactive channels:
    - tries distributed lock
    - if lock is already held (healthy Redis), queue this turn
    - if Redis is unavailable/degraded, fail-open (parallel processing allowed)
    - no drain/redispatch and no async summary side effects by default
    """
    ttl = int(lock_ttl_seconds or (get_env("PROCESSING_LOCK_TTL_SECONDS") or "90"))
    pending_queue_max_messages = int(get_env("PENDING_QUEUE_MAX_MESSAGES") or "20")
    pending_queue_ttl_seconds = int(get_env("PENDING_QUEUE_TTL_SECONDS") or "300")

    def _lock_key(client_id: str, user_id: str) -> str:
        return f"conv:processing:{client_id}:{user_id}"

    def _pending_queue_key(client_id: str, user_id: str) -> str:
        return f"conv:pending:{client_id}:{user_id}"

    def _extract_message_text(payload_data: dict) -> str:
        text = str((payload_data or {}).get("_runtime_message_text") or "").strip()
        if text:
            return text
        if (payload_data or {}).get("streaming"):
            return "[streaming_message]"
        return "[queued_runtime_payload]"

    def _try_acquire(client_id: str, user_id: str, trace_id: str):
        async def _impl():
            redis_client = await _await_maybe(get_redis_client_fn())
            if not redis_client:
                log_fn(trace_id, "Single-flight lock skipped: Redis unavailable", "warning", user_id, client_id=client_id)
                return False, True
            key = _lock_key(client_id, user_id)
            lock_result = await redis_guard.execute_async(
                op_name="single_flight_lock_setnx",
                fn=lambda: redis_client.set(key, "1", nx=True, ex=ttl),
                fallback=False,
            )
            if not lock_result.ok:
                return False, True
            if not lock_result.value:
                return False, False
            return True, False
        return _impl()

    def _enqueue_pending(client_id: str, user_id: str, trace_id: str, payload_data: dict) -> bool:
        async def _impl():
            redis_client = await _await_maybe(get_redis_client_fn())
            if not redis_client:
                log_fn(trace_id, "Pending queue skipped: Redis unavailable", "warning", user_id, client_id=client_id)
                return False

            queue_key = _pending_queue_key(client_id, user_id)
            entry = {
                "trace_id": trace_id,
                "ts": datetime.datetime.now().isoformat(),
                "message_text": _extract_message_text(payload_data),
                "payload_data": payload_data,
            }
            push_result = await redis_guard.execute_async(
                op_name="pending_queue_rpush",
                fn=lambda: redis_client.rpush(queue_key, json.dumps(entry)),
                fallback=0,
            )
            if not push_result.ok:
                return False

            _ = await redis_guard.execute_async(
                op_name="pending_queue_ltrim",
                fn=lambda: redis_client.ltrim(queue_key, -pending_queue_max_messages, -1),
                fallback=False,
            )
            _ = await redis_guard.execute_async(
                op_name="pending_queue_expire",
                fn=lambda: redis_client.expire(queue_key, pending_queue_ttl_seconds),
                fallback=False,
            )
            return True
        return _impl()

    def _release(client_id: str, user_id: str, trace_id: str):
        async def _impl():
            redis_client = await _await_maybe(get_redis_client_fn())
            if not redis_client:
                return
            key = _lock_key(client_id, user_id)
            await redis_guard.execute_async(
                op_name="single_flight_lock_delete",
                fn=lambda: redis_client.delete(key),
                fallback=0,
            )
        return _impl()

    def _no_op(*_args, **_kwargs):
        return None

    def _no_queue(*_args, **_kwargs):
        return False

    def _empty_list(*_args, **_kwargs):
        return []

    def _empty_merge(*_args, **_kwargs):
        return {}, 0, 0

    def _summary_disabled(*_args, **_kwargs):
        return False, "disabled"

    return ConversationRuntime(
        log_fn=log_fn,
        mark_degraded_state_fn=_no_op,
        try_acquire_lock_fn=_try_acquire,
        enqueue_pending_fn=_enqueue_pending,
        release_lock_fn=_release,
        drain_pending_fn=_empty_list,
        build_merged_payload_fn=_empty_merge,
        should_trigger_summary_fn=_summary_disabled,
        enqueue_summary_job_fn=_no_op,
        redispatch_fn=_no_op,
    )
