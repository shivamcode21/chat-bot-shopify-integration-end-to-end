"""
APScheduler setup for running cron jobs.

Cron Jobs:
1. Agent Mode Auto-Switch: Runs every hour to switch inactive agent modes to bot mode
2. Conversation Resolution: Runs every 4 hours to mark conversations as resolved/unresolved
3. Product Vector Sync: Runs WEEKLY (Sundays 3:00 AM UTC) as fallback for missed webhooks
   - Primary sync is via Shopify webhooks (products/create, products/update, products/delete)
   - Weekly cron ensures data consistency if any webhooks are missed
4. Cancellation Aversion Classifier: Runs every 1 hour to classify pending cancellation aversion events
5. Conversation Analytics: Runs every 2 hours to analyze completed conversations via LLM prompt
4. Conversion Tagging: Runs every hour to classify ended conversations with conversion tags
"""
import asyncio
import logging
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timezone as dt_timezone
from fashion_bot.env_loader import get_bool
from .agent_mode_auto_switch import auto_switch_inactive_agent_modes
from .conversation_resolution_job import process_conversation_resolutions
from .product_vector_sync_job import product_vector_delta_sync
from .bestseller_refresh_job import bestseller_refresh_monthly
from .shopify_token_refresh_job import refresh_shopify_token
from .cancellation_aversion_classifier_job import classify_pending_cancellation_events
from .conversation_analytics_job import analyze_pending_conversations
# conversation_scan_event scheduler disabled — inactivity scheduler only.
# from .conversation_event_scheduler import (
#     CONVERSATION_EVENT_INTERVAL_MINUTES,
#     emit_recent_conversation_event,
# )
from .conversation_inactivity_scheduler import (
    CONVERSATION_INACTIVITY_INTERVAL_MINUTES,
    emit_inactive_conversation_events,
)
from .conversation_inactivity_cleanup_job import (
    CONVERSATION_INACTIVITY_CURSOR_CLEANUP_HOUR_UTC,
    CONVERSATION_INACTIVITY_CURSOR_CLEANUP_MINUTE_UTC,
    cleanup_conversation_inactivity_cursors,
)
from .session_count_sync_job import session_count_sync_daily
from .bloomerce_edited_sync_job import bloomerce_edited_sync_daily
#from .conversion_tag_job import process_conversion_tags

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler = None

# Bounded concurrency for heavy async cron jobs. We share the API event
# loop now, so a single semaphore prevents two heavy jobs from competing
# for DB connections (and request handlers) at the same time.
_HEAVY_JOB_SEMAPHORE: "asyncio.Semaphore | None" = None


def _get_heavy_job_semaphore() -> asyncio.Semaphore:
    global _HEAVY_JOB_SEMAPHORE
    if _HEAVY_JOB_SEMAPHORE is None:
        _HEAVY_JOB_SEMAPHORE = asyncio.Semaphore(1)
    return _HEAVY_JOB_SEMAPHORE


def _guarded(async_fn):
    """Wrap a heavy async cron job in the shared heavy-job semaphore."""
    async def _runner():
        async with _get_heavy_job_semaphore():
            return await async_fn()
    _runner.__name__ = getattr(async_fn, "__name__", "guarded_job")
    return _runner


def _log_scheduler_event(event):
    job_id = getattr(event, "job_id", None)
    run_time = getattr(event, "scheduled_run_time", None)
    scheduled_at = run_time.isoformat() if run_time else None

    if event.code == EVENT_JOB_MISSED:
        logger.warning(
            "[CRON_SCHEDULER] MISSED job_id=%s scheduled_run_time=%s",
            job_id,
            scheduled_at,
        )
    elif event.code == EVENT_JOB_MAX_INSTANCES:
        logger.warning("[CRON_SCHEDULER] MAX_INSTANCES job_id=%s", job_id)
    elif event.code == EVENT_JOB_ERROR:
        logger.error(
            "[CRON_SCHEDULER] ERROR job_id=%s scheduled_run_time=%s exception=%s",
            job_id,
            scheduled_at,
            getattr(event, "exception", None),
            exc_info=getattr(event, "exception", None),
        )
    elif event.code == EVENT_JOB_EXECUTED:
        logger.info(
            "[CRON_SCHEDULER] EXECUTED job_id=%s scheduled_run_time=%s",
            job_id,
            scheduled_at,
        )


async def start_cron_scheduler():
    """
    Start the AsyncIOScheduler for all cron jobs on the running event loop.
    Every pod may start the in-process scheduler. Individual heavy jobs use
    Redis leases inside the job body, so only one pod actually executes each
    protected cron run. This avoids stale scheduler-leader locks preventing
    all pods from scheduling.

    Async because AsyncIOScheduler binds to the loop running at .start();
    this must be invoked from the FastAPI startup event so cron jobs share
    the same event loop as request handlers.
    """
    global _scheduler

    if _scheduler is not None:
        logger.warning("[CRON_SCHEDULER] Scheduler already running")
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone='UTC', event_loop=asyncio.get_running_loop())
    _scheduler.add_listener(
        _log_scheduler_event,
        EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
    )

    # conversation_scan_event disabled — only inactivity events are enqueued.
    # if get_bool("ENABLE_CONVERSATION_EVENT_SCHEDULER", False):
    #     _scheduler.add_job(
    #         func=emit_recent_conversation_event,
    #         trigger=IntervalTrigger(
    #             minutes=max(1, CONVERSATION_EVENT_INTERVAL_MINUTES),
    #             timezone='UTC',
    #         ),
    #         id='conversation_event_scheduler',
    #         name='Emit recent conversation event to Dramatiq',
    #         replace_existing=True,
    #         misfire_grace_time=120,
    #         coalesce=True,
    #         max_instances=1,
    #     )

    if get_bool("ENABLE_CONVERSATION_INACTIVITY_SCHEDULER", False):
        _scheduler.add_job(
            func=emit_inactive_conversation_events,
            trigger=IntervalTrigger(
                minutes=max(1, CONVERSATION_INACTIVITY_INTERVAL_MINUTES),
                timezone='UTC',
            ),
            id='conversation_inactivity_scheduler',
            name='Emit inactive conversation events to Dramatiq',
            replace_existing=True,
            misfire_grace_time=120,
            coalesce=True,
            max_instances=1,
        )

    if get_bool("ENABLE_CONVERSATION_INACTIVITY_CURSOR_CLEANUP_SCHEDULER", False):
        _scheduler.add_job(
            func=cleanup_conversation_inactivity_cursors,
            trigger=CronTrigger(
                hour=CONVERSATION_INACTIVITY_CURSOR_CLEANUP_HOUR_UTC,
                minute=CONVERSATION_INACTIVITY_CURSOR_CLEANUP_MINUTE_UTC,
                timezone='UTC',
            ),
            id='conversation_inactivity_cursor_cleanup',
            name='Cleanup old conversation inactivity cursors',
            replace_existing=True,
            misfire_grace_time=900,
            coalesce=True,
            max_instances=1,
        )


    
    # Add job: Auto-switch inactive agent modes to bot mode
    # Runs every hour at :00 minutes (e.g., 1:00, 2:00, 3:00...)
    _scheduler.add_job(
         func=auto_switch_inactive_agent_modes,
         trigger=CronTrigger(hour='*', minute=0, timezone='UTC'),
         id='auto_switch_agent_modes',
         name='Auto-switch inactive agent modes to bot mode',
         replace_existing=True,
         misfire_grace_time=300,  # Allow 5 minutes grace if job misses schedule
         coalesce=True,
         max_instances=1
    )
    
    # Add job: Process ended conversations and mark as resolved/unresolved
    # Runs every 4 hours (at 0:00, 4:00, 8:00, 12:00, 16:00, 20:00 UTC)
    # _scheduler.add_job(
    #      func=process_conversation_resolutions,
    #      trigger=CronTrigger(hour='*/4', minute=0, timezone='UTC'),  # Every 4 hours
    #      id='conversation_resolution',
    #      name='Process conversation resolutions (resolved/unresolved)',
    #      replace_existing=True,
    #      misfire_grace_time=1800,  # Allow 30 minutes grace if job misses schedule
    #      max_instances=1  # Only allow 1 instance at a time
    # )
    
    # Add job: Weekly delta sync products to Upstash VectorDB (FALLBACK)
    # Primary sync is handled via Shopify webhooks (products/create, products/update, products/delete).
    # This weekly job serves as a fallback to catch any missed webhooks and ensure data consistency.
    # Runs every Sunday at 3:00 AM UTC.
    # Wrapped in the heavy-job semaphore so it cannot overlap conversation
    # analytics on the shared API event loop.
    _scheduler.add_job(
         func=_guarded(product_vector_delta_sync),
         trigger=CronTrigger(day_of_week='sun', hour=3, minute=0, timezone='UTC'),  # Every Sunday at 3:00 AM UTC
         id='product_vector_weekly_sync',
         name='Weekly delta sync products to Upstash VectorDB (fallback for missed webhooks)',
         replace_existing=True,
         misfire_grace_time=3600,  # Allow 1 hour grace if job misses schedule
         coalesce=True,
         max_instances=1  # Only allow 1 instance at a time
    )
    
    # Add job: Bestseller refresh every 3 days — reconcile the Upstash `bestseller`
    # flag with the last 3 months of Shopify sales (adds new bestsellers,
    # clears products that are no longer bestselling).
    # Registered on every pod like the other heavy crons; the Redis lease in the
    # job body (`bestseller_refresh_lock`) guarantees a single pod actually runs
    # it. Wrapped in the heavy-job semaphore so it cannot overlap the weekly
    # vector sync / analytics.
    #
    # start_date is MANDATORY here. An IntervalTrigger without one defaults to
    # `now + interval`, so the 3-day countdown re-anchors to process start on
    # every boot and is discarded on the next restart. This service restarts
    # several times a day, so the countdown never reached zero and the job did
    # not fire at all between Jul and Aug 2026. Anchoring to a fixed past
    # instant snaps firing onto an absolute 3-day grid (04:00 UTC) that every
    # pod computes identically and no restart can reset.
    _scheduler.add_job(
         func=_guarded(bestseller_refresh_monthly),
         trigger=IntervalTrigger(
             days=3,
             start_date=datetime(2026, 1, 1, 4, 0, tzinfo=dt_timezone.utc),
             timezone='UTC',
         ),
         id='bestseller_refresh_monthly',
         name='Bestseller refresh every 3 days (Shopify 3-month sales → Upstash bestseller flag)',
         replace_existing=True,
         misfire_grace_time=3600,
         coalesce=True,
         max_instances=1
    )

    # PAUSED: Shopify token refresh every 12 hours
    # Shopify client_credentials tokens expire every 24 hours.
    # Runs twice daily at 00:00 and 12:00 UTC for safety margin.
    # _scheduler.add_job(
    #      func=refresh_shopify_token,
    #      trigger=CronTrigger(hour='0,12', minute=0, timezone='UTC'),  # Every 12 hours
    #      id='shopify_token_refresh',
    #      name='Shopify access token refresh (every 12 hours)',
    #      replace_existing=True,
    #      misfire_grace_time=300,  # Allow 5 minutes grace
    #      max_instances=1
    # )
    
    # PAUSED: Cancellation aversion classifier
    # Runs every 1 hour to process events older than 90 minutes
    # Uses LLM to determine if cancellation was averted/cancelled/abandoned
    # _scheduler.add_job(
    #      func=classify_pending_cancellation_events,
    #      trigger=CronTrigger(hour='*', minute=0, timezone='UTC'),  # Every hour at :00
    #      id='cancellation_aversion_classifier',
    #      name='Classify pending cancellation aversion events (every 1 hour)',
    #      replace_existing=True,
    #      misfire_grace_time=600,  # Allow 10 minutes grace if job misses schedule
    #      max_instances=1  # Only allow 1 instance at a time
    # )
    
    # Add job: Analyze completed conversations with central analytics prompt
    # Runs 3x daily at 13:00, 17:00 and 23:00 IST (07:30, 11:30 and 17:30 UTC).
    # Picks up ended conversations (>90 min inactive) not yet analyzed.
    # Wrapped in the heavy-job semaphore so analytics and the weekly vector
    # sync cannot overlap on the shared event loop.
    _scheduler.add_job(
         func=_guarded(analyze_pending_conversations),
         trigger=CronTrigger(hour='7,11,17', minute=30, timezone='UTC'),
         id='conversation_analytics',
         name='Analyze completed conversations via LLM prompt (3x daily)',
         replace_existing=True,
         misfire_grace_time=900,
         coalesce=True,
         max_instances=1
    )
    # Add job: Conversion tagging — classify ended conversations with conversion tags
    # Runs every hour at :30 minutes (offset from agent-mode switch at :00)
    # Picks up conversations that ended >90 min ago and haven't been tagged yet
    # _scheduler.add_job(
    #      func=process_conversion_tags,
    #      trigger=CronTrigger(hour='*', minute=30, timezone='UTC'),  # Every hour at :30
    #      id='conversion_tagging',
    #      name='Conversion tagging for ended conversations (hourly)',
    #      replace_existing=True,
    #      misfire_grace_time=600,  # Allow 10 minutes grace
    #      max_instances=1  # Only allow 1 instance at a time
    # )

    # Add job: Daily session count sync — fetch unique sessions from Shopify
    # ShopifyQL for each client and persist to client_sessions table.
    # Runs daily at 01:00 UTC (6:30 AM IST) — gives Shopify time to finalize the day's data.
    # Redis lease in job body ensures single-pod execution.
    _scheduler.add_job(
         func=_guarded(session_count_sync_daily),
         trigger=CronTrigger(hour=1, minute=0, timezone='UTC'),
         id='session_count_sync_daily',
         name='Daily Shopify session count sync via ShopifyQL',
         replace_existing=True,
         misfire_grace_time=3600,
         coalesce=True,
         max_instances=1,
    )

    _scheduler.add_job(
         func=_guarded(bloomerce_edited_sync_daily),
         trigger=CronTrigger(hour=1, minute=30, timezone='UTC'),
         id='bloomerce_edited_sync_daily',
         name='Daily sync of BLOOMERCE_EDITED orders to Postgres (7:00 AM IST)',
         replace_existing=True,
         misfire_grace_time=3600,
         coalesce=True,
         max_instances=1,
    )

    _scheduler.start()
    
    print(f"\n{'='*80}")
    print(f"[CRON_SCHEDULER] ⚠️ Scheduler started at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  1. Weekly product vector delta sync (Sundays at 3:00 AM UTC - fallback for missed webhooks)")
    print(f"  1b. Bestseller refresh (every 3 days - single pod via Redis lease)")
    print(f"  2. Shopify token refresh (every 12 hours — 00:00 & 12:00 UTC)")
    print(f"  3. Auto-switch inactive agent modes (every hour)")
    print(f"  4. Process conversation resolutions (every 4 hours)")
    print(f"  5. Classify pending cancellation aversion events (every 1 hour)")
    print(f"  6. Conversation analytics via LLM prompt (every 2 hours)")
    print(f"{'='*80}\n")
    
    for job in _scheduler.get_jobs():
        logger.info(
            "[CRON_SCHEDULER] Job scheduled id=%s next_run_time=%s trigger=%s",
            job.id,
            job.next_run_time.isoformat() if job.next_run_time else None,
            job.trigger,
        )

    logger.info("[CRON_SCHEDULER] Cron scheduler started")
    
    return _scheduler

async def stop_cron_scheduler():
    """Stop the AsyncIOScheduler."""
    global _scheduler

    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("[CRON_SCHEDULER] Scheduler stopped")

def get_scheduler():
    """Get the current scheduler instance."""
    return _scheduler

def get_job_status():
    """
    Get status of all scheduled jobs.
    
    Returns:
        dict with job information
    """
    if not _scheduler:
        return {
            'scheduler_running': False,
            'message': 'Scheduler not started'
        }
    
    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'name': job.name,
            'next_run_time': job.next_run_time.isoformat() if job.next_run_time else None,
            'trigger': str(job.trigger)
        })
    
    return {
        'scheduler_running': _scheduler.running,
        'jobs': jobs
    }

# For testing manually
if __name__ == "__main__":
    async def _main():
        print("Starting scheduler for testing...")
        await start_cron_scheduler()
        print("\nScheduler is running. Will test for 2 minutes...")
        print("Press Ctrl+C to stop\n")
        try:
            await asyncio.sleep(120)
        except KeyboardInterrupt:
            print("\nStopping scheduler...")
        finally:
            await stop_cron_scheduler()
            print("Done!")

    asyncio.run(_main())
