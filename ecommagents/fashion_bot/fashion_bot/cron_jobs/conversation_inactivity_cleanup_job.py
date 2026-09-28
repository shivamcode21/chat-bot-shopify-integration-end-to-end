"""Cleanup old conversation inactivity cursor rows."""

from __future__ import annotations

import logging
from typing import Any, Dict

from fashion_bot.cron_jobs.cron_lock import (
    acquire_cron_lock,
    get_cron_lock_owner,
    release_cron_lock,
)
from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.env_loader import get_int
from fashion_bot.monitoring.otel_metrics import track_cron

logger = logging.getLogger(__name__)

CONVERSATION_INACTIVITY_CURSOR_CLEANUP_HOUR_UTC: int = get_int(
    "CONVERSATION_INACTIVITY_CURSOR_CLEANUP_HOUR_UTC",
    19,
)
CONVERSATION_INACTIVITY_CURSOR_CLEANUP_MINUTE_UTC: int = get_int(
    "CONVERSATION_INACTIVITY_CURSOR_CLEANUP_MINUTE_UTC",
    30,
)
CONVERSATION_INACTIVITY_CURSOR_CLEANUP_DONE_HOURS: int = get_int(
    "CONVERSATION_INACTIVITY_CURSOR_CLEANUP_DONE_HOURS",
    24,
)
CONVERSATION_INACTIVITY_CURSOR_CLEANUP_FAILED_DAYS: int = get_int(
    "CONVERSATION_INACTIVITY_CURSOR_CLEANUP_FAILED_DAYS",
    7,
)

LOCK_KEY = "conversation_inactivity_cursor_cleanup_lock"
LOCK_TTL_SECONDS = 600


async def _ensure_processing_tables(cur) -> None:
    from fashion_bot.Tables.conversation_inactivity_processing_table import DDL

    await cur.execute(DDL)


@track_cron("conversation_inactivity_cursor_cleanup", items_key="cursors_deleted")
async def cleanup_conversation_inactivity_cursors() -> Dict[str, Any]:
    """Delete old durable cursors after their retry/debug windows expire."""
    lease = None

    try:
        lease = await acquire_cron_lock(lock_key=LOCK_KEY, ttl_seconds=LOCK_TTL_SECONDS)
        if not lease:
            current_owner = await get_cron_lock_owner(LOCK_KEY)
            logger.info(
                "[CONVERSATION_INACTIVITY_CLEANUP] Cleanup already running or lock unavailable. "
                "owner=%s",
                current_owner or "unknown",
            )
            return {
                "success": True,
                "skipped": True,
                "cursors_deleted": 0,
                "message": "Skipped: cleanup already running",
            }

        logger.info(
            "[CONVERSATION_INACTIVITY_CLEANUP] Cleanup lock acquired by %s",
            lease.owner_id,
        )

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await _ensure_processing_tables(cur)

                await cur.execute(
                    """
                    DELETE FROM conversation_inactivity_cursors
                    WHERE status = 'done'
                      AND updated_at < NOW() - (%s * INTERVAL '1 hour')
                    """,
                    (CONVERSATION_INACTIVITY_CURSOR_CLEANUP_DONE_HOURS,),
                )
                done_deleted = cur.rowcount or 0

                await cur.execute(
                    """
                    DELETE FROM conversation_inactivity_cursors
                    WHERE status IN ('error', 'publish_failed')
                      AND updated_at < NOW() - (%s * INTERVAL '1 day')
                    """,
                    (CONVERSATION_INACTIVITY_CURSOR_CLEANUP_FAILED_DAYS,),
                )
                failed_deleted = cur.rowcount or 0

            await conn.commit()

        total_deleted = done_deleted + failed_deleted
        logger.info(
            "[CONVERSATION_INACTIVITY_CLEANUP] Deleted cursor rows: "
            "done=%d failed_or_publish_failed=%d total=%d",
            done_deleted,
            failed_deleted,
            total_deleted,
        )
        return {
            "success": True,
            "done_deleted": done_deleted,
            "failed_deleted": failed_deleted,
            "cursors_deleted": total_deleted,
            "message": f"Deleted {total_deleted} cursor row(s)",
        }

    except Exception as exc:
        logger.error(
            "[CONVERSATION_INACTIVITY_CLEANUP] Cleanup failed: %s",
            exc,
            exc_info=True,
        )
        return {
            "success": False,
            "cursors_deleted": 0,
            "error": str(exc),
        }
    finally:
        if lease:
            try:
                await release_cron_lock(lease)
            except Exception as exc:
                logger.warning(
                    "[CONVERSATION_INACTIVITY_CLEANUP] Failed to release cleanup lock: %s",
                    exc,
                )
