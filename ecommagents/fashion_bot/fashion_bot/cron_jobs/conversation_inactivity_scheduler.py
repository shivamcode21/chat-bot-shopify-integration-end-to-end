"""Emit events for conversations that have gone quiet with unread inbound messages."""

from __future__ import annotations

import os

# One-shot `python -m` runs use a direct DB connection. Warming the async pool
# (USE_DB_POOL=true) can hang against remote Postgres during cron startup.
if __name__ == "__main__":
    os.environ.setdefault("USE_DB_POOL", "false")

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fashion_bot.cron_jobs.cron_lock import (
    acquire_cron_lock,
    get_cron_lock_owner,
    release_cron_lock,
)
from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.env_loader import get_int
from fashion_bot.monitoring.otel_metrics import track_cron
from fashion_bot.workers.event_publishers import publish_conversation_inactivity_event

logger = logging.getLogger(__name__)

CONVERSATION_INACTIVITY_INTERVAL_MINUTES: int = get_int(
    "CONVERSATION_INACTIVITY_INTERVAL_MINUTES",
    15,
)
CONVERSATION_INACTIVITY_THRESHOLD_MINUTES: int = get_int(
    "CONVERSATION_INACTIVITY_THRESHOLD_MINUTES",
    10,
)
CONVERSATION_INACTIVITY_BATCH_SIZE: int = get_int(
    "CONVERSATION_INACTIVITY_BATCH_SIZE",
    100,
)
CONVERSATION_INACTIVITY_REQUEUE_AFTER_MINUTES: int = get_int(
    "CONVERSATION_INACTIVITY_REQUEUE_AFTER_MINUTES",
    30,
)
CONVERSATION_INACTIVITY_OPEN_WINDOW_MINUTES: int = get_int(
    "CONVERSATION_INACTIVITY_OPEN_WINDOW_MINUTES",
    90,
)
CONVERSATION_INACTIVITY_CANDIDATE_SCAN_LIMIT: int = get_int(
    "CONVERSATION_INACTIVITY_CANDIDATE_SCAN_LIMIT",
    1000,
)

LOCK_KEY = "conversation_inactivity_scheduler_lock"


def _dt_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def _ensure_processing_tables(cur) -> None:
    from fashion_bot.Tables.conversation_inactivity_processing_table import DDL

    await cur.execute(DDL)


async def _fetch_inactive_candidates(
    cur,
    *,
    threshold_minutes: int,
    open_window_minutes: int,
    candidate_scan_limit: int,
    batch_size: int,
    requeue_after_minutes: int,
) -> List[Dict[str, Any]]:
    await cur.execute(
        """
        WITH latest_inbound_per_conversation AS (
            SELECT DISTINCT ON (m.conversation_id)
                m.conversation_id,
                m.message_id,
                m.created_at
            FROM messages m
            WHERE m.message_side = 'user_to_system'
              AND m.created_at >= NOW() - (%s * INTERVAL '1 minute')
            ORDER BY m.conversation_id, m.created_at DESC, m.message_id DESC
        ),
        candidate_conversations AS (
            SELECT
                c.conversation_id,
                c.client_id,
                c.phone,
                c.channel_type,
                li.created_at AS last_message_at,
                li.message_id AS to_inbound_message_id,
                li.created_at AS to_inbound_message_at
            FROM latest_inbound_per_conversation li
            JOIN conversations c
              ON c.conversation_id = li.conversation_id
             AND c.status = 'active'
            WHERE li.created_at < NOW() - (%s * INTERVAL '1 minute')
            ORDER BY li.created_at ASC, li.message_id ASC
            LIMIT %s
        )
        SELECT
            ca.conversation_id::text AS conversation_id,
            ca.client_id::text AS client_id,
            ca.phone,
            ca.channel_type,
            ca.last_message_at,
            ca.to_inbound_message_id::text AS to_inbound_message_id,
            ca.to_inbound_message_at,
            cic.last_processed_inbound_message_id::text AS from_cursor_message_id,
            cic.last_processed_inbound_message_at AS from_cursor_at
        FROM candidate_conversations ca
        LEFT JOIN conversation_inactivity_cursors cic
          ON cic.conversation_id = ca.conversation_id
        WHERE (
            cic.conversation_id IS NULL
            OR cic.last_processed_inbound_message_at IS NULL
            OR ca.to_inbound_message_at > cic.last_processed_inbound_message_at
            OR (
                ca.to_inbound_message_at = cic.last_processed_inbound_message_at
                AND ca.to_inbound_message_id > cic.last_processed_inbound_message_id
            )
        )
          AND (
            cic.status IS NULL
            OR cic.status IN ('done', 'error', 'publish_failed')
            OR (
                cic.status IN ('queued', 'processing')
                AND COALESCE(cic.queued_at, cic.started_at, cic.updated_at)
                    < NOW() - (%s * INTERVAL '1 minute')
            )
          )
        ORDER BY ca.to_inbound_message_at ASC, ca.to_inbound_message_id ASC
        LIMIT %s
        """,
        (
            open_window_minutes,
            threshold_minutes,
            candidate_scan_limit,
            requeue_after_minutes,
            batch_size,
        ),
    )
    return [dict(row) for row in await cur.fetchall()]


async def _claim_candidate(cur, candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    await cur.execute(
        """
        INSERT INTO conversation_inactivity_cursors (
            conversation_id,
            client_id,
            phone,
            channel_type,
            last_queued_inbound_message_at,
            last_queued_inbound_message_id,
            status,
            queued_at,
            retry_count,
            last_error,
            created_at,
            updated_at
        )
        VALUES (
            %s::uuid, %s::uuid, %s, %s,
            %s, %s::uuid,
            'queued', NOW(), 0, NULL, NOW(), NOW()
        )
        ON CONFLICT (conversation_id) DO UPDATE
        SET
            client_id = EXCLUDED.client_id,
            phone = EXCLUDED.phone,
            channel_type = EXCLUDED.channel_type,
            last_queued_inbound_message_at = EXCLUDED.last_queued_inbound_message_at,
            last_queued_inbound_message_id = EXCLUDED.last_queued_inbound_message_id,
            status = 'queued',
            queued_at = NOW(),
            retry_count = conversation_inactivity_cursors.retry_count + 1,
            last_error = NULL,
            updated_at = NOW()
        WHERE
            conversation_inactivity_cursors.last_processed_inbound_message_at IS NULL
            OR EXCLUDED.last_queued_inbound_message_at
                > conversation_inactivity_cursors.last_processed_inbound_message_at
            OR (
                EXCLUDED.last_queued_inbound_message_at
                    = conversation_inactivity_cursors.last_processed_inbound_message_at
                AND EXCLUDED.last_queued_inbound_message_id
                    > conversation_inactivity_cursors.last_processed_inbound_message_id
            )
        RETURNING
            conversation_id::text,
            client_id::text,
            phone,
            channel_type,
            last_processed_inbound_message_id::text AS from_cursor_message_id,
            last_processed_inbound_message_at AS from_cursor_at,
            last_queued_inbound_message_id::text AS to_inbound_message_id,
            last_queued_inbound_message_at AS to_inbound_message_at
        """,
        (
            candidate["conversation_id"],
            candidate["client_id"],
            candidate.get("phone"),
            candidate.get("channel_type"),
            candidate["to_inbound_message_at"],
            candidate["to_inbound_message_id"],
        ),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def _mark_publish_failed(conversation_id: str, error: str) -> None:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE conversation_inactivity_cursors
                SET status = 'publish_failed',
                    last_error = %s,
                    updated_at = NOW()
                WHERE conversation_id = %s::uuid
                  AND status = 'queued'
                """,
                (error[:1000], conversation_id),
            )
        await conn.commit()


async def _claim_inactive_conversation_ranges(
    *,
    threshold_minutes: int,
    open_window_minutes: int,
    candidate_scan_limit: int,
    batch_size: int,
    requeue_after_minutes: int,
) -> List[Dict[str, Any]]:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await _ensure_processing_tables(cur)
            candidates = await _fetch_inactive_candidates(
                cur,
                threshold_minutes=threshold_minutes,
                open_window_minutes=open_window_minutes,
                candidate_scan_limit=candidate_scan_limit,
                batch_size=batch_size,
                requeue_after_minutes=requeue_after_minutes,
            )
            claimed: List[Dict[str, Any]] = []
            for candidate in candidates:
                claim = await _claim_candidate(cur, candidate)
                if claim:
                    claimed.append(claim)
        await conn.commit()
    return claimed


@track_cron("conversation_inactivity_event_scheduler", items_key="conversation_events")
async def emit_inactive_conversation_events() -> Dict[str, Any]:
    """Scheduled producer: claim quiet inbound ranges and enqueue worker events."""
    interval_minutes = max(1, CONVERSATION_INACTIVITY_INTERVAL_MINUTES)
    threshold_minutes = max(0, CONVERSATION_INACTIVITY_THRESHOLD_MINUTES)
    open_window_minutes = max(
        threshold_minutes + 1,
        CONVERSATION_INACTIVITY_OPEN_WINDOW_MINUTES,
    )
    batch_size = max(1, CONVERSATION_INACTIVITY_BATCH_SIZE)
    candidate_scan_limit = max(batch_size, CONVERSATION_INACTIVITY_CANDIDATE_SCAN_LIMIT)
    requeue_after_minutes = max(1, CONVERSATION_INACTIVITY_REQUEUE_AFTER_MINUTES)
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
                "[CONVERSATION_INACTIVITY_SCHEDULER] already running owner=%s",
                current_owner or "unknown",
            )
            return {"success": True, "skipped": True, "reason": "lock_held"}

        claimed = await _claim_inactive_conversation_ranges(
            threshold_minutes=threshold_minutes,
            open_window_minutes=open_window_minutes,
            candidate_scan_limit=candidate_scan_limit,
            batch_size=batch_size,
            requeue_after_minutes=requeue_after_minutes,
        )
        queued = 0
        emitted_at = datetime.now(timezone.utc).isoformat()

        for claim in claimed:
            payload = {
                "conversation_id": claim["conversation_id"],
                "client_id": claim["client_id"],
                "phone": claim.get("phone"),
                "channel_type": claim.get("channel_type"),
                "from_cursor_at": _dt_iso(claim.get("from_cursor_at")),
                "from_cursor_message_id": claim.get("from_cursor_message_id"),
                "to_inbound_message_at": _dt_iso(claim.get("to_inbound_message_at")),
                "to_inbound_message_id": claim.get("to_inbound_message_id"),
                "threshold_minutes": threshold_minutes,
                "open_window_minutes": open_window_minutes,
                "candidate_scan_limit": candidate_scan_limit,
                "emitted_at": emitted_at,
                "trace_id": trace_id,
            }
            result = await publish_conversation_inactivity_event(payload)
            if result.get("queued"):
                queued += 1
            else:
                await _mark_publish_failed(
                    claim["conversation_id"],
                    f"event not queued: {result}",
                )

        logger.info(
            "[CONVERSATION_INACTIVITY_SCHEDULER] claimed=%s queued=%s threshold_minutes=%s open_window_minutes=%s candidate_scan_limit=%s",
            len(claimed),
            queued,
            threshold_minutes,
            open_window_minutes,
            candidate_scan_limit,
        )
        return {
            "success": True,
            "conversation_events": queued,
            "claimed": len(claimed),
            "threshold_minutes": threshold_minutes,
            "open_window_minutes": open_window_minutes,
            "candidate_scan_limit": candidate_scan_limit,
        }
    except Exception as exc:
        logger.exception("[CONVERSATION_INACTIVITY_SCHEDULER] failed: %s", exc)
        return {"success": False, "error": str(exc), "conversation_events": 0}
    finally:
        if lease:
            await release_cron_lock(lease)


if __name__ == "__main__":
    import asyncio
    import json
    import sys

    from fashion_bot.env_loader import bootstrap_environment

    bootstrap_environment()

    result = asyncio.run(emit_inactive_conversation_events())
    print(json.dumps(result, default=str, indent=2))
    sys.exit(0 if result.get("success") else 1)
