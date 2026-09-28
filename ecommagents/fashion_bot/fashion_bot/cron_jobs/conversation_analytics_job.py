"""
Conversation Analytics Job
==========================
Runs twice daily to analyze completed conversations using the central
analytics prompt.

What it does:
- Finds ended conversations (>90 min inactive) not yet analyzed
- Sends each conversation to the LLM with a multi-dimensional analytics prompt
- Stores structured results in the conversation_analytics table

This runs alongside the real-time tool-based cancellation_aversion_tracker
so both approaches can be compared.

Native async — invoked directly by AsyncIOScheduler on the app event loop.
"""

import asyncio
import logging
from typing import Dict, Any

from fashion_bot.env_loader import get_int
from fashion_bot.monitoring.otel_metrics import track_cron
from fashion_bot.cron_jobs.cron_lock import acquire_cron_lock, release_cron_lock, get_cron_lock_owner

logger = logging.getLogger(__name__)


LOOKBACK_DAYS: int = 2
CONVERSATION_ANALYTICS_BATCH_SIZE: int = get_int("CONVERSATION_ANALYTICS_BATCH_SIZE", 5)
CONVERSATION_ANALYTICS_TIMEOUT_SECONDS: int = get_int("CONVERSATION_ANALYTICS_TIMEOUT_SECONDS", 1800)
LOCK_KEY = "conversation_analytics_lock"
LOCK_TTL_SECONDS = 2100  # 35 minutes - must stay above CONVERSATION_ANALYTICS_TIMEOUT_SECONDS
# so the lock can't expire mid-run and let an overlapping run start.


@track_cron("conversation_analytics", items_key="conversations_analyzed")
async def analyze_pending_conversations() -> Dict[str, Any]:
    """
    Analyze ended conversations that have not been processed yet.

    Only looks at the last LOOKBACK_DAYS (default 2) days of conversations
    so the scheduler stays focused on recent traffic.
    """
    lease = None
    # Defined up front (not inside the try) so any exception/cancellation
    # path can report how much was actually analyzed before it happened.
    progress = {"analyzed": 0, "batches": 0}

    try:
        lease = await acquire_cron_lock(lock_key=LOCK_KEY, ttl_seconds=LOCK_TTL_SECONDS)
        if not lease:
            current_owner = await get_cron_lock_owner(LOCK_KEY)
            logger.info(
                "[CONVERSATION_ANALYTICS_CRON] Analytics already running or lock backend unavailable. "
                "owner=%s",
                current_owner or "unknown",
            )
            return {
                "success": True,
                "skipped": True,
                "conversations_analyzed": 0,
                "message": "Skipped: analytics already running",
            }

        logger.info("[CONVERSATION_ANALYTICS_CRON] Analytics lock acquired by %s", lease.owner_id)

        from fashion_bot.analytics.conversation_analyzer import run_pending_analyses

        logger.info(
            "[CONVERSATION_ANALYTICS_CRON] Starting analysis sweep "
            "(last %d days, batch_size=%d, timeout=%ds)...",
            LOOKBACK_DAYS,
            CONVERSATION_ANALYTICS_BATCH_SIZE,
            CONVERSATION_ANALYTICS_TIMEOUT_SECONDS,
        )

        async def _run_with_budget():
            while True:
                n = await run_pending_analyses(
                    batch_size=CONVERSATION_ANALYTICS_BATCH_SIZE,
                    lookback_days=LOOKBACK_DAYS,
                )
                if n <= 0:
                    break
                progress["analyzed"] += n
                progress["batches"] += 1
                logger.info(
                    "[CONVERSATION_ANALYTICS_CRON] Backlog drain progress — batches=%d total_analyzed=%d",
                    progress["batches"],
                    progress["analyzed"],
                )
            return progress["analyzed"]

        count = await asyncio.wait_for(
            _run_with_budget(),
            timeout=CONVERSATION_ANALYTICS_TIMEOUT_SECONDS,
        )

        result = {
            "success": True,
            "conversations_analyzed": count,
            "message": f"Analyzed {count} conversation(s)",
        }

        if count > 0:
            logger.info(
                f"[CONVERSATION_ANALYTICS_CRON] Analyzed {count} conversation(s)"
            )
        else:
            logger.debug(
                "[CONVERSATION_ANALYTICS_CRON] No pending conversations to analyze"
            )

        return result

    except asyncio.TimeoutError:
        logger.warning(
            "[CONVERSATION_ANALYTICS_CRON] Analysis timed out after %ds (analyzed %d before cutoff); "
            "next scheduled run will continue remaining conversations.",
            CONVERSATION_ANALYTICS_TIMEOUT_SECONDS,
            progress["analyzed"],
        )
        return {
            "success": False,
            "conversations_analyzed": progress["analyzed"],
            "error": "conversation_analytics_timeout",
        }
    except asyncio.CancelledError as exc:
        logger.warning(
            "[CONVERSATION_ANALYTICS_CRON] Analysis was cancelled (analyzed %d before cancellation): %s",
            progress["analyzed"],
            exc,
            exc_info=True,
        )
        return {
            "success": False,
            "conversations_analyzed": progress["analyzed"],
            "error": "conversation_analytics_cancelled",
        }
    except Exception as exc:
        logger.error(
            f"[CONVERSATION_ANALYTICS_CRON] Analysis failed (analyzed {progress['analyzed']} before error): {exc}",
            exc_info=True,
        )
        return {
            "success": False,
            "conversations_analyzed": progress["analyzed"],
            "error": str(exc),
        }
    finally:
        if lease:
            try:
                await release_cron_lock(lease)
            except Exception as exc:
                logger.warning("[CONVERSATION_ANALYTICS_CRON] Failed to release analytics lock: %s", exc)
