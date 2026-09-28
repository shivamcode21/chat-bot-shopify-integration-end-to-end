from __future__ import annotations

import asyncio
import copy
import datetime
import inspect
import json
import logging
import uuid
from typing import Callable

from fashion_bot.env_loader import get_bool, get_env

logger = logging.getLogger("gupshup_runtime_support")


class GupshupRuntimeSupport:
    """
    Owns WhatsApp runtime helper behavior:
    - single-flight lock lifecycle
    - pending queue enqueue/drain/merge
    - in-process async summary queue worker
    """

    def __init__(
        self,
        *,
        get_redis_client_fn: Callable[[], object],
        redis_guard,
        log_fn: Callable[..., None],
        mark_degraded_state_fn: Callable[[str, str, str, str], None],
        get_state_fn: Callable[[str, str], dict],
        update_state_fn: Callable[[str, str, dict], None],
        generate_trace_id_fn: Callable[[], str],
    ):
        self._get_redis_client = get_redis_client_fn
        self._redis_guard = redis_guard
        self._log = log_fn
        self._mark_degraded = mark_degraded_state_fn
        self._get_state = get_state_fn
        self._update_state = update_state_fn
        self._generate_trace_id = generate_trace_id_fn

        self.enable_single_flight_merge = get_bool("ENABLE_SINGLE_FLIGHT_MERGE", True)
        self.processing_lock_ttl_seconds = int(get_env("PROCESSING_LOCK_TTL_SECONDS") or "90")
        self.pending_queue_max_messages = int(get_env("PENDING_QUEUE_MAX_MESSAGES") or "20")
        self.pending_queue_max_chars = int(get_env("PENDING_QUEUE_MAX_CHARS") or "4000")
        self.enable_async_summarizer = get_bool("ENABLE_ASYNC_SUMMARIZER", True)
        self.summary_every_turn_pairs = int(get_env("SUMMARY_EVERY_TURN_PAIRS") or "8")
        self.summary_lock_ttl_seconds = int(get_env("SUMMARY_LOCK_TTL_SECONDS") or "60")
        self.summary_max_lines = int(get_env("SUMMARY_MAX_LINES") or "10")
        self.summary_max_chars = int(get_env("SUMMARY_MAX_CHARS") or "1800")

        self._active_summary_workers = set()
        self._summary_worker_lock = asyncio.Lock()

    @staticmethod
    def processing_lock_key(client_id: str, sender_phone: str) -> str:
        return f"conv:processing:{client_id}:{sender_phone}"

    @staticmethod
    def pending_queue_key(client_id: str, sender_phone: str) -> str:
        return f"conv:pending:{client_id}:{sender_phone}"

    @staticmethod
    def summary_lock_key(client_id: str, phone: str) -> str:
        return f"summary:lock:{client_id}:{phone}"

    @staticmethod
    def extract_message_text_for_merge(payload_data: dict) -> str:
        payload = payload_data.get("payload", {}) or {}
        message_type = payload.get("type", "") or ""
        if message_type == "text":
            return (payload.get("payload", {}) or {}).get("text", "") or ""
        if message_type in ("quick_reply", "button"):
            inner = payload.get("payload", {}) or {}
            return inner.get("text", "") or inner.get("postbackText", "") or f"[{message_type}]"
        if message_type in ("audio", "video", "image"):
            return f"[{message_type} message]"
        return f"[{message_type or 'unknown'} message]"

    async def _amerge_text_with_stored_media(
        self, client_id: str, sender_phone: str, trace_id: str, payload_data: dict
    ) -> str:
        """Merge text for a queued message, preserving any media link.

        A queued media message is re-dispatched as a plain-text merged payload,
        so it never reaches the webhook's media handling. Store it here or the
        customer's photo expires with the Gupshup URL, unseen.
        """
        text = self.extract_message_text_for_merge(payload_data)
        try:
            from fashion_bot.utils.media_storage import aprepare_inbound_media

            merged_text, _metadata = await aprepare_inbound_media(
                payload_data,
                placeholder_text=text,
                client_id=client_id,
                phone=sender_phone,
                trace_id=trace_id,
            )
            return merged_text
        except Exception as media_err:
            self._log(
                trace_id,
                f"Queued media storage failed: {media_err}",
                "warning",
                sender_phone,
                client_id,
            )
            return text

    async def _await_maybe(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def try_acquire_processing_lock(self, client_id: str, sender_phone: str, trace_id: str) -> tuple[bool, bool]:
        if not self.enable_single_flight_merge:
            return False, False
        rc = await self._await_maybe(self._get_redis_client())
        if not rc:
            self._log(trace_id, "Single-flight lock skipped: Redis unavailable", "warning", sender_phone, client_id)
            return False, True

        lock_key = self.processing_lock_key(client_id, sender_phone)
        result = await self._redis_guard.execute_async(
            op_name="single_flight_lock_setnx",
            fn=lambda: rc.set(lock_key, "1", nx=True, ex=self.processing_lock_ttl_seconds),
            fallback=False,
        )
        if not result.ok:
            self._log(
                trace_id,
                f"Single-flight lock degraded for {sender_phone}: {result.error}",
                "warning",
                sender_phone,
                client_id,
            )
            return False, True
        return bool(result.value), False

    async def release_processing_lock(self, client_id: str, sender_phone: str, trace_id: str):
        if not self.enable_single_flight_merge:
            return
        rc = await self._await_maybe(self._get_redis_client())
        if not rc:
            return
        lock_key = self.processing_lock_key(client_id, sender_phone)
        result = await self._redis_guard.execute_async(
            op_name="single_flight_lock_delete",
            fn=lambda: rc.delete(lock_key),
            fallback=0,
        )
        if not result.ok:
            self._log(trace_id, f"Failed to release processing lock: {result.error}", "warning", sender_phone, client_id)

    async def enqueue_pending_message(self, client_id: str, sender_phone: str, trace_id: str, payload_data: dict) -> bool:
        rc = await self._await_maybe(self._get_redis_client())
        if not rc:
            self._log(trace_id, "Pending queue enqueue skipped: Redis unavailable", "warning", sender_phone, client_id)
            return False

        queue_key = self.pending_queue_key(client_id, sender_phone)
        entry = {
            "trace_id": trace_id,
            "ts": datetime.datetime.now().isoformat(),
            "message_text": await self._amerge_text_with_stored_media(
                client_id, sender_phone, trace_id, payload_data
            ),
            "payload_data": payload_data,
        }
        encoded = json.dumps(entry)
        push_result = await self._redis_guard.execute_async(
            op_name="pending_queue_rpush",
            fn=lambda: rc.rpush(queue_key, encoded),
            fallback=0,
        )
        if not push_result.ok:
            self._log(trace_id, f"Pending queue enqueue degraded: {push_result.error}", "warning", sender_phone, client_id)
            return False
        _ = await self._redis_guard.execute_async(
            op_name="pending_queue_ltrim",
            fn=lambda: rc.ltrim(queue_key, -self.pending_queue_max_messages, -1),
            fallback=False,
        )
        self._log(trace_id, f"Queued pending message for merge (queue={queue_key})", "info", sender_phone, client_id)
        return True

    async def drain_pending_queue(self, client_id: str, sender_phone: str) -> list[dict]:
        rc = await self._await_maybe(self._get_redis_client())
        if not rc:
            return []
        queue_key = self.pending_queue_key(client_id, sender_phone)
        read_result = await self._redis_guard.execute_async(
            op_name="pending_queue_lrange",
            fn=lambda: rc.lrange(queue_key, 0, -1),
            fallback=[],
        )
        if not read_result.ok:
            return []
        _ = await self._redis_guard.execute_async(
            op_name="pending_queue_delete",
            fn=lambda: rc.delete(queue_key),
            fallback=0,
        )
        rows = read_result.value or []
        entries = []
        for row in rows:
            try:
                entries.append(json.loads(row))
            except Exception:
                continue
        return entries

    def build_merged_payload(self, base_data: dict, pending_entries: list[dict]) -> tuple[dict, int, int]:
        texts = []
        merged_ids = 0
        total_chars = 0
        for entry in pending_entries:
            txt = (entry.get("message_text") or "").strip()
            if not txt:
                continue
            if total_chars + len(txt) > self.pending_queue_max_chars:
                break
            texts.append(txt)
            total_chars += len(txt)
            merged_ids += 1

        merged_text = "\n".join(texts).strip()
        if not merged_text:
            return {}, 0, 0

        merged = copy.deepcopy(base_data)
        payload = merged.setdefault("payload", {})
        payload["type"] = "text"
        payload_payload = payload.setdefault("payload", {})
        payload_payload["text"] = merged_text
        payload["id"] = f"merged_{uuid.uuid4().hex[:10]}"
        return merged, merged_ids, total_chars

    def should_trigger_summary(self, state_snapshot: dict, reply_text: str = "") -> tuple[bool, str]:
        if not self.enable_async_summarizer:
            return False, "disabled"

        messages = state_snapshot.get("messages", []) or []
        ai_count = 0
        for m in messages:
            if getattr(m, "type", "") == "ai" or m.__class__.__name__ == "AIMessage":
                ai_count += 1

        if ai_count > 0 and ai_count % self.summary_every_turn_pairs == 0:
            return True, "periodic"

        txt = (reply_text or "").lower()
        critical_markers = [
            "order placed",
            "order cancelled",
            "canceled",
            "escalated",
            "support team",
            "return initiated",
            "exchange initiated",
        ]
        if any(marker in txt for marker in critical_markers):
            return True, "critical"
        return False, "none"

    def enqueue_summary_job(self, sender_phone: str, client_id: str, state_snapshot: dict, trigger_reason: str, trace_id: str):
        rc = self._get_redis_client()
        if not rc:
            self._mark_degraded(sender_phone, client_id, "summary", "summary_skipped_due_to_redis")
            return
        try:
            job = {
                "job_id": uuid.uuid4().hex[:12],
                "trace_id": trace_id,
                "phone": sender_phone,
                "client_id": client_id,
                "trigger_reason": trigger_reason,
                "target_msg_idx": len(state_snapshot.get("messages", []) or []),
                "enqueued_at": datetime.datetime.now().isoformat(),
            }
            key = f"summary:jobs:{client_id}"
            result = self._redis_guard.execute(
                op_name="summary_queue_rpush",
                fn=lambda: rc.rpush(key, json.dumps(job)),
                fallback=0,
            )
            if not result.ok:
                self._mark_degraded(sender_phone, client_id, "summary", "summary_skipped_due_to_redis")
                self._log(trace_id, f"Summary enqueue degraded: {result.error}", "warning", sender_phone, client_id=client_id)
            else:
                self._log(trace_id, f"📝 Summary queued reason={trigger_reason} target_idx={job['target_msg_idx']}", "info", sender_phone, client_id=client_id)
                try:
                    asyncio.create_task(self.schedule_summary_worker(client_id))
                except Exception as sched_err:
                    self._log(trace_id, f"Summary worker schedule failed: {sched_err}", "warning", sender_phone, client_id=client_id)
        except Exception as summary_err:
            self._mark_degraded(sender_phone, client_id, "summary", "summary_skipped_due_to_redis")
            self._log(trace_id, f"Summary enqueue failed: {summary_err}", "warning", sender_phone, client_id=client_id)

    def _build_incremental_summary(self, messages: list, start_idx: int, end_idx: int) -> str:
        lines = []
        for msg in (messages or [])[start_idx:end_idx]:
            role = getattr(msg, "type", "") or msg.__class__.__name__.lower()
            content = str(getattr(msg, "content", msg)).strip().replace("\n", " ")
            if not content:
                continue
            if len(content) > 220:
                content = f"{content[:220]}..."
            role_label = "User" if role in ("human", "HumanMessage".lower()) else "Bot"
            lines.append(f"{role_label}: {content}")
            if len(lines) >= self.summary_max_lines:
                break
        return "\n".join(lines)

    async def schedule_summary_worker(self, client_id: str):
        async with self._summary_worker_lock:
            if client_id in self._active_summary_workers:
                return
            self._active_summary_workers.add(client_id)
        try:
            await self.run_summary_worker(client_id)
        finally:
            async with self._summary_worker_lock:
                self._active_summary_workers.discard(client_id)

    async def run_summary_worker(self, client_id: str):
        rc = await self._await_maybe(self._get_redis_client())
        if not rc:
            return
        queue_key = f"summary:jobs:{client_id}"
        while True:
            pop_result = await self._redis_guard.execute_async(
                op_name="summary_queue_lpop",
                fn=lambda: rc.lpop(queue_key),
                fallback=None,
            )
            if not pop_result.ok or not pop_result.value:
                return
            try:
                job = json.loads(pop_result.value)
            except Exception:
                continue
            await self.process_summary_job(job)

    async def process_summary_job(self, job: dict):
        client_id = job.get("client_id")
        sender_phone = job.get("phone")
        trace_id = job.get("trace_id") or self._generate_trace_id()
        target_idx = int(job.get("target_msg_idx") or 0)
        if not client_id or not sender_phone or target_idx <= 0:
            return

        rc = await self._await_maybe(self._get_redis_client())
        if not rc:
            self._mark_degraded(sender_phone, client_id, "summary", "summary_skipped_due_to_redis")
            return

        lock_key = self.summary_lock_key(client_id, sender_phone)
        lock_result = await self._redis_guard.execute_async(
            op_name="summary_lock_setnx",
            fn=lambda: rc.set(lock_key, "1", nx=True, ex=self.summary_lock_ttl_seconds),
            fallback=False,
        )
        if not lock_result.ok or not lock_result.value:
            return

        try:
            state = await self._await_maybe(self._get_state(sender_phone, client_id)) or {}
            messages = state.get("messages", []) or []
            if not messages:
                return
            safe_target = min(target_idx, len(messages))
            watermark = int(state.get("summary_applied_upto_msg_idx") or 0)
            if safe_target <= watermark:
                return

            delta_summary = self._build_incremental_summary(messages, watermark, safe_target)
            if not delta_summary:
                state["summary_applied_upto_msg_idx"] = safe_target
                await self._await_maybe(self._update_state(sender_phone, client_id, state))
                return

            conversation_context = state.get("conversation_context") or {}
            prev_summary = (conversation_context.get("rolling_summary") or "").strip()
            merged_summary = f"{prev_summary}\n{delta_summary}".strip() if prev_summary else delta_summary
            if len(merged_summary) > self.summary_max_chars:
                merged_summary = merged_summary[-self.summary_max_chars:]

            conversation_context["rolling_summary"] = merged_summary
            conversation_context["summary_updated_at"] = datetime.datetime.now().isoformat()
            state["conversation_context"] = conversation_context
            state["summary_applied_upto_msg_idx"] = safe_target
            await self._await_maybe(self._update_state(sender_phone, client_id, state))
            self._log(trace_id, f"📝 Summary updated upto idx={safe_target}", "info", sender_phone, client_id=client_id)
        except Exception as summary_err:
            self._mark_degraded(sender_phone, client_id, "summary", "summary_skipped_due_to_redis")
            self._log(trace_id, f"Summary job failed: {summary_err}", "warning", sender_phone, client_id=client_id)
        finally:
            _ = await self._redis_guard.execute_async(
                op_name="summary_lock_delete",
                fn=lambda: rc.delete(lock_key),
                fallback=0,
            )
