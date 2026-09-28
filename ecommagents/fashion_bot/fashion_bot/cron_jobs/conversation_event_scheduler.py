"""Conversation event scheduler.

Reads recently active conversations every N minutes and emits one Dramatiq event
for downstream consumers. The consumer only logs for now.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from fashion_bot.cron_jobs.cron_lock import (
    acquire_cron_lock,
    get_cron_lock_owner,
    release_cron_lock,
)
from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.env_loader import get_int
from fashion_bot.workers.event_publishers import publish_conversation_scan_event

logger = logging.getLogger(__name__)

CONVERSATION_EVENT_INTERVAL_MINUTES: int = get_int(
    "CONVERSATION_EVENT_INTERVAL_MINUTES",
    5,
)
CONVERSATION_EVENT_BATCH_SIZE: int = get_int(
    "CONVERSATION_EVENT_BATCH_SIZE",
    100,
)
LOCK_KEY = "conversation_event_scheduler_lock"


async def _fetch_recent_conversations(
    *,
    interval_minutes: int,
    batch_size: int,
) -> List[Dict[str, Any]]:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    conversation_id::text AS conversation_id,
                    client_id::text AS client_id,
                    phone,
                    channel_type,
                    first_message,
                    updated_at
                FROM conversations
                WHERE updated_at >= NOW() - (%s * INTERVAL '1 minute')
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (interval_minutes, batch_size),
            )
            rows = await cur.fetchall()

    return [dict(row) for row in rows]


async def emit_recent_conversation_event() -> Dict[str, Any]:
    """Scheduled producer: read conversations and enqueue a scan event."""
    interval_minutes = max(1, CONVERSATION_EVENT_INTERVAL_MINUTES)
    batch_size = max(1, CONVERSATION_EVENT_BATCH_SIZE)
    trace_id = uuid.uuid4().hex[:8]
    lease = None

    try:
        lease = await acquire_cron_lock(
            lock_key=LOCK_KEY,
            ttl_seconds=max(120, interval_minutes * 120),
        )
        if not lease:
            current_owner = await get_cron_lock_owner(LOCK_KEY)
            logger.info(
                "[CONVERSATION_EVENT_SCHEDULER] already running owner=%s",
                current_owner or "unknown",
            )
            return {"success": True, "skipped": True, "reason": "lock_held"}

        conversations = await _fetch_recent_conversations(
            interval_minutes=interval_minutes,
            batch_size=batch_size,
        )
        conversation_ids = [row["conversation_id"] for row in conversations]
        payload = {
            "conversation_ids": conversation_ids,
            "conversation_count": len(conversation_ids),
            "interval_minutes": interval_minutes,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": trace_id,
        }
        result = await publish_conversation_scan_event(payload)
        logger.info(
            "[CONVERSATION_EVENT_SCHEDULER] emitted count=%s interval_minutes=%s result=%s",
            len(conversation_ids),
            interval_minutes,
            result,
        )
        return {
            "success": True,
            "conversation_count": len(conversation_ids),
            "queued": bool(result.get("queued")),
        }
    except Exception as exc:
        logger.exception("[CONVERSATION_EVENT_SCHEDULER] failed: %s", exc)
        return {"success": False, "error": str(exc)}
    finally:
        if lease:
            await release_cron_lock(lease)
