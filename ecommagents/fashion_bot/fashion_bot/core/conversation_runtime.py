"""
Central conversation runtime orchestration.

Owns single-flight lock/queue, inline summary scheduling, degraded fail-open behavior,
and per-turn redis/db operation metrics.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Literal, Optional

from fashion_bot.rollbar_config import report_error
from fashion_bot.utils.turn_metrics import close_turn_metrics, open_turn_metrics

logger = logging.getLogger(__name__)


@dataclass
class RuntimeContext:
    channel: Literal["whatsapp", "web", "streamlit"]
    client_id: str
    user_id: str
    inbound_payload: Dict[str, Any]
    trace_id: str
    degraded_mode: bool = False
    degraded_components: List[str] = field(default_factory=list)
    redis_calls_total: int = 0
    redis_calls_by_op: Dict[str, int] = field(default_factory=dict)
    db_calls_total: int = 0
    db_calls_by_component: Dict[str, int] = field(default_factory=dict)


@dataclass
class RuntimeResult:
    handled: bool
    queued: bool = False
    reply_text: str = ""
    state_snapshot: Optional[Dict[str, Any]] = None
    degraded_mode: bool = False
    degraded_components: List[str] = field(default_factory=list)
    redis_calls_total: int = 0
    redis_calls_by_op: Dict[str, int] = field(default_factory=dict)
    db_calls_total: int = 0
    db_calls_by_component: Dict[str, int] = field(default_factory=dict)


class ConversationRuntime:
    """
    Single orchestration wrapper around per-turn execution.
    """

    def __init__(
        self,
        *,
        log_fn: Callable[..., None],
        mark_degraded_state_fn: Callable[[str, str, str, str], Any],
        # Returns: (lock_granted, single_flight_unavailable_for_turn)
        # - lock_granted=True: this turn has single-flight ownership.
        # - single_flight_unavailable_for_turn=True: fail-open path (do not enqueue).
        try_acquire_lock_fn: Callable[[str, str, str], Any],
        enqueue_pending_fn: Callable[[str, str, str, Dict[str, Any]], Any],
        release_lock_fn: Callable[[str, str, str], Any],
        drain_pending_fn: Callable[[str, str], Any],
        build_merged_payload_fn: Callable[[Dict[str, Any], List[Dict[str, Any]]], tuple[Dict[str, Any], int, int]],
        should_trigger_summary_fn: Callable[[Dict[str, Any], str], Any],
        enqueue_summary_job_fn: Callable[[str, str, Dict[str, Any], str, str], Any],
        redispatch_fn: Callable[[Dict[str, Any], str, str], Any],
    ):
        self.log = log_fn
        self.mark_degraded_state = mark_degraded_state_fn
        self.try_acquire_lock = try_acquire_lock_fn
        self.enqueue_pending = enqueue_pending_fn
        self.release_lock = release_lock_fn
        self.drain_pending = drain_pending_fn
        self.build_merged_payload = build_merged_payload_fn
        self.should_trigger_summary = should_trigger_summary_fn
        self.enqueue_summary_job = enqueue_summary_job_fn
        self.redispatch = redispatch_fn

    @staticmethod
    async def _await_maybe(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _summarize_pending_entries(pending: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Compact, log-friendly summary of queued entries considered for merge."""
        out: List[Dict[str, Any]] = []
        for idx, entry in enumerate(pending or [], start=1):
            txt = str((entry or {}).get("message_text") or "").replace("\n", " ").strip()
            preview = f"{txt[:140]}..." if len(txt) > 140 else txt
            out.append(
                {
                    "idx": idx,
                    "trace_id": (entry or {}).get("trace_id"),
                    "ts": (entry or {}).get("ts"),
                    "chars": len(txt),
                    "preview": preview,
                }
            )
        return out

    @staticmethod
    def _pending_entries_log_text(entries: List[Dict[str, Any]], max_items: int = 10) -> str:
        if not entries:
            return "none"
        parts: List[str] = []
        for item in entries[:max_items]:
            parts.append(
                f"{item.get('idx')}|{item.get('trace_id') or '-'}|{item.get('chars') or 0}c|{item.get('preview') or ''}"
            )
        if len(entries) > max_items:
            parts.append(f"...(+{len(entries) - max_items} more)")
        return " || ".join(parts)

    async def run_turn(
        self,
        channel: Literal["whatsapp", "web", "streamlit"],
        client_id: str,
        user_id: str,
        inbound_payload: dict,
        execute_fn: Callable[[RuntimeContext], Awaitable[RuntimeResult]],
        trace_id: str,
    ) -> RuntimeResult:
        started = time.time()
        ctx = RuntimeContext(
            channel=channel,
            client_id=client_id,
            user_id=user_id,
            inbound_payload=inbound_payload,
            trace_id=trace_id,
        )
        lock_acquired = False
        # Per-turn (not process-global) accounting: see utils/turn_metrics.py.
        metrics, metrics_token = open_turn_metrics()
        try:
            lock_granted, single_flight_unavailable = await self._await_maybe(
                self.try_acquire_lock(client_id, user_id, trace_id)
            )
            should_enqueue_pending = (not lock_granted) and (not single_flight_unavailable)
            should_allow_parallel_processing = (not lock_granted) and single_flight_unavailable

            if single_flight_unavailable:
                ctx.degraded_mode = True
                if "single_flight" not in ctx.degraded_components:
                    ctx.degraded_components.append("single_flight")
                try:
                    await self._await_maybe(
                        self.mark_degraded_state(user_id, client_id, "single_flight", "single_flight_degraded")
                    )
                except Exception:
                    pass
            if lock_granted:
                lock_acquired = True
            elif should_enqueue_pending:
                queued = await self._await_maybe(
                    self.enqueue_pending(client_id, user_id, trace_id, inbound_payload)
                )
                if not queued:
                    ctx.degraded_mode = True
                    if "single_flight" not in ctx.degraded_components:
                        ctx.degraded_components.append("single_flight")
                    try:
                        await self._await_maybe(
                            self.mark_degraded_state(user_id, client_id, "single_flight", "single_flight_degraded")
                        )
                    except Exception:
                        pass
                    self.log(
                        trace_id,
                        "Single-flight queueing failed; failing open and processing this turn now",
                        "warning",
                        user_id,
                        client_id=client_id,
                    )
                else:
                    queued_reply = (
                        "I am still processing your previous message. "
                        "This message has been queued."
                    )
                    return RuntimeResult(handled=False, queued=True, reply_text=queued_reply)
            elif should_allow_parallel_processing:
                self.log(
                    trace_id,
                    "Single-flight unavailable for this turn; allowing parallel processing (fail-open)",
                    "warning",
                    user_id,
                    client_id=client_id,
                )

            result = await execute_fn(ctx)

            # Inline summary scheduling
            try:
                snapshot = result.state_snapshot or {}
                should_summary, reason = await self._await_maybe(
                    self.should_trigger_summary(snapshot, result.reply_text or "")
                )
                if should_summary:
                    await self._await_maybe(
                        self.enqueue_summary_job(user_id, client_id, snapshot, reason, trace_id)
                    )
            except Exception as summary_err:
                ctx.degraded_mode = True
                if "summary" not in ctx.degraded_components:
                    ctx.degraded_components.append("summary")
                try:
                    await self._await_maybe(
                        self.mark_degraded_state(user_id, client_id, "summary", "summary_skipped_due_to_redis")
                    )
                except Exception:
                    pass
                self.log(trace_id, f"Summary trigger check failed: {summary_err}", "warning", user_id, client_id=client_id)

            result.degraded_mode = result.degraded_mode or ctx.degraded_mode
            result.degraded_components = list(set((result.degraded_components or []) + ctx.degraded_components))
            ctx.redis_calls_total = metrics.redis_calls
            ctx.redis_calls_by_op = dict(metrics.redis_calls_by_op)
            ctx.db_calls_total = metrics.db_calls
            result.redis_calls_total = metrics.redis_calls
            result.redis_calls_by_op = dict(metrics.redis_calls_by_op)
            result.db_calls_total = metrics.db_calls
            result.db_calls_by_component = {"connection_acquire": metrics.db_calls}
            return result
        except Exception as run_err:
            # Hard failure: Rollbar
            report_error(
                "ConversationRuntime turn failed",
                level="error",
                exc_info=(type(run_err), run_err, run_err.__traceback__),
                trace_id=trace_id,
                client_id=client_id,
                user_id=user_id,
                channel=channel,
            )
            raise
        finally:
            close_turn_metrics(metrics_token)
            try:
                if lock_acquired:
                    pending = await self._await_maybe(self.drain_pending(client_id, user_id))
                    if pending:
                        pending_debug = self._summarize_pending_entries(pending)
                        merged_payload, merged_count, merged_chars = self.build_merged_payload(inbound_payload, pending)
                        self.log(
                            trace_id,
                            f"🧩 Drain-all merge: count={merged_count}, chars={merged_chars}, pending_entries={len(pending)}",
                            "info",
                            user_id,
                            client_id=client_id,
                        )
                        if merged_payload and merged_count > 0:
                            merged_trace_id = f"m{trace_id[:7]}"
                            if isinstance(merged_payload, dict):
                                merged_payload["_runtime_queue_debug"] = {
                                    "scheduled_from_trace_id": trace_id,
                                    "merged_trace_id": merged_trace_id,
                                    "merged_count": merged_count,
                                    "merged_chars": merged_chars,
                                    "queued_entries": pending_debug,
                                    "scheduled_at_epoch_ms": int(time.time() * 1000),
                                }
                            self.log(
                                trace_id,
                                f"⏭️ Queued processing scheduled: merged_trace_id={merged_trace_id}, "
                                f"merged_count={merged_count}, considered={self._pending_entries_log_text(pending_debug)}",
                                "info",
                                user_id,
                                client_id=client_id,
                            )
                            await self._await_maybe(
                                self.redispatch(merged_payload, merged_trace_id, client_id)
                            )
            except Exception as merge_err:
                try:
                    await self._await_maybe(
                        self.mark_degraded_state(user_id, client_id, "single_flight", "single_flight_degraded")
                    )
                except Exception:
                    pass
                self.log(trace_id, f"Pending merge handling failed: {merge_err}", "warning", user_id, client_id=client_id)
            finally:
                if lock_acquired:
                    await self._await_maybe(self.release_lock(client_id, user_id, trace_id))
                elapsed_ms = int((time.time() - started) * 1000)
                self.log(
                    trace_id,
                    f"🧮 runtime_metrics redis_calls={metrics.redis_calls} db_calls={metrics.db_calls} elapsed_ms={elapsed_ms}",
                    "info",
                    user_id,
                    client_id=client_id,
                )

    @staticmethod
    def _extract_reply_from_result(result: Optional[Dict[str, Any]]) -> str:
        """Best-effort extraction of assistant text from graph result.

        Only returns content from AIMessage objects to prevent echoing
        a user's HumanMessage back as the bot reply.
        """
        if not isinstance(result, dict):
            return ""
        from langchain_core.messages import AIMessage
        messages = result.get("messages") or []
        if messages:
            final_message = messages[-1]
            if isinstance(final_message, AIMessage):
                return str(final_message.content or "")
        return str(result.get("customer_message") or "")

    async def run_turn_stream(
        self,
        channel: Literal["whatsapp", "web", "streamlit"],
        client_id: str,
        user_id: str,
        inbound_payload: dict,
        execute_stream_fn: Callable[[RuntimeContext], AsyncGenerator[Dict[str, Any], None]],
        trace_id: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Streaming variant of run_turn.

        Yields stream events from execute_stream_fn and appends a final
        `runtime_metrics` event at completion.
        """
        started = time.time()
        ctx = RuntimeContext(
            channel=channel,
            client_id=client_id,
            user_id=user_id,
            inbound_payload=inbound_payload,
            trace_id=trace_id,
        )
        lock_acquired = False
        # Per-turn (not process-global) accounting: see utils/turn_metrics.py.
        metrics, metrics_token = open_turn_metrics()
        stream_result = RuntimeResult(handled=True, reply_text="", state_snapshot=None)
        try:
            lock_granted, single_flight_unavailable = await self._await_maybe(
                self.try_acquire_lock(client_id, user_id, trace_id)
            )
            should_enqueue_pending = (not lock_granted) and (not single_flight_unavailable)
            should_allow_parallel_processing = (not lock_granted) and single_flight_unavailable

            if single_flight_unavailable:
                ctx.degraded_mode = True
                if "single_flight" not in ctx.degraded_components:
                    ctx.degraded_components.append("single_flight")
                try:
                    await self._await_maybe(
                        self.mark_degraded_state(user_id, client_id, "single_flight", "single_flight_degraded")
                    )
                except Exception:
                    pass

            if lock_granted:
                lock_acquired = True
            elif should_enqueue_pending:
                queued = await self._await_maybe(
                    self.enqueue_pending(client_id, user_id, trace_id, inbound_payload)
                )
                if not queued:
                    ctx.degraded_mode = True
                    if "single_flight" not in ctx.degraded_components:
                        ctx.degraded_components.append("single_flight")
                    try:
                        await self._await_maybe(
                            self.mark_degraded_state(user_id, client_id, "single_flight", "single_flight_degraded")
                        )
                    except Exception:
                        pass
                    self.log(
                        trace_id,
                        "Single-flight queueing failed; failing open and processing this turn now",
                        "warning",
                        user_id,
                        client_id=client_id,
                    )
                else:
                    queued_reply = (
                        "I am still processing your previous message. "
                        "This message has been queued."
                    )
                    yield {"type": "queued", "queued": True, "message": queued_reply}
                    # Do not expose queued text as final customer-visible response.
                    yield {"type": "end", "full_response": "", "queued": True}
                    stream_result = RuntimeResult(handled=False, queued=True, reply_text="")
                    return
            elif should_allow_parallel_processing:
                self.log(
                    trace_id,
                    "Single-flight unavailable for this turn; allowing parallel processing (fail-open)",
                    "warning",
                    user_id,
                    client_id=client_id,
                )

            async for event in execute_stream_fn(ctx):
                if isinstance(event, dict):
                    if "state_snapshot" in event and isinstance(event.get("state_snapshot"), dict):
                        stream_result.state_snapshot = event.get("state_snapshot")
                    if "reply_text" in event and str(event.get("reply_text") or "").strip():
                        stream_result.reply_text = str(event.get("reply_text") or "")

                    event_type = event.get("type")
                    if event_type == "token":
                        token_text = str(event.get("content") or "")
                        if token_text:
                            stream_result.reply_text += token_text
                    elif event_type == "end":
                        full_response = str(event.get("full_response") or "").strip()
                        if full_response:
                            stream_result.reply_text = full_response
                        result_obj = event.get("result")
                        if isinstance(result_obj, dict):
                            stream_result.state_snapshot = result_obj
                            if not stream_result.reply_text:
                                stream_result.reply_text = self._extract_reply_from_result(result_obj)
                yield event

            # Inline summary scheduling for stream turns
            try:
                snapshot = stream_result.state_snapshot or {}
                should_summary, reason = await self._await_maybe(
                    self.should_trigger_summary(snapshot, stream_result.reply_text or "")
                )
                if should_summary:
                    await self._await_maybe(
                        self.enqueue_summary_job(user_id, client_id, snapshot, reason, trace_id)
                    )
            except Exception as summary_err:
                ctx.degraded_mode = True
                if "summary" not in ctx.degraded_components:
                    ctx.degraded_components.append("summary")
                try:
                    await self._await_maybe(
                        self.mark_degraded_state(user_id, client_id, "summary", "summary_skipped_due_to_redis")
                    )
                except Exception:
                    pass
                self.log(trace_id, f"Summary trigger check failed: {summary_err}", "warning", user_id, client_id=client_id)

            stream_result.degraded_mode = stream_result.degraded_mode or ctx.degraded_mode
            stream_result.degraded_components = list(set((stream_result.degraded_components or []) + ctx.degraded_components))
            ctx.redis_calls_total = metrics.redis_calls
            ctx.redis_calls_by_op = dict(metrics.redis_calls_by_op)
            ctx.db_calls_total = metrics.db_calls
            stream_result.redis_calls_total = metrics.redis_calls
            stream_result.redis_calls_by_op = dict(metrics.redis_calls_by_op)
            stream_result.db_calls_total = metrics.db_calls
            stream_result.db_calls_by_component = {"connection_acquire": metrics.db_calls}
        except Exception as run_err:
            report_error(
                "ConversationRuntime stream turn failed",
                level="error",
                exc_info=(type(run_err), run_err, run_err.__traceback__),
                trace_id=trace_id,
                client_id=client_id,
                user_id=user_id,
                channel=channel,
            )
            raise
        finally:
            close_turn_metrics(metrics_token)
            try:
                if lock_acquired:
                    pending = await self._await_maybe(self.drain_pending(client_id, user_id))
                    if pending:
                        pending_debug = self._summarize_pending_entries(pending)
                        merged_payload, merged_count, merged_chars = self.build_merged_payload(inbound_payload, pending)
                        self.log(
                            trace_id,
                            f"🧩 Drain-all merge: count={merged_count}, chars={merged_chars}, pending_entries={len(pending)}",
                            "info",
                            user_id,
                            client_id=client_id,
                        )
                        if merged_payload and merged_count > 0:
                            merged_trace_id = f"m{trace_id[:7]}"
                            if isinstance(merged_payload, dict):
                                merged_payload["_runtime_queue_debug"] = {
                                    "scheduled_from_trace_id": trace_id,
                                    "merged_trace_id": merged_trace_id,
                                    "merged_count": merged_count,
                                    "merged_chars": merged_chars,
                                    "queued_entries": pending_debug,
                                    "scheduled_at_epoch_ms": int(time.time() * 1000),
                                }
                            self.log(
                                trace_id,
                                f"⏭️ Queued processing scheduled: merged_trace_id={merged_trace_id}, "
                                f"merged_count={merged_count}, considered={self._pending_entries_log_text(pending_debug)}",
                                "info",
                                user_id,
                                client_id=client_id,
                            )
                            await self._await_maybe(
                                self.redispatch(merged_payload, merged_trace_id, client_id)
                            )
            except Exception as merge_err:
                try:
                    await self._await_maybe(
                        self.mark_degraded_state(user_id, client_id, "single_flight", "single_flight_degraded")
                    )
                except Exception:
                    pass
                self.log(trace_id, f"Pending merge handling failed: {merge_err}", "warning", user_id, client_id=client_id)
            finally:
                if lock_acquired:
                    await self._await_maybe(self.release_lock(client_id, user_id, trace_id))

                elapsed_ms = int((time.time() - started) * 1000)
                self.log(
                    trace_id,
                    f"🧮 runtime_metrics redis_calls={metrics.redis_calls} db_calls={metrics.db_calls} elapsed_ms={elapsed_ms}",
                    "info",
                    user_id,
                    client_id=client_id,
                )
