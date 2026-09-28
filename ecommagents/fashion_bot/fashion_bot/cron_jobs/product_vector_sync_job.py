"""
Product Vector Sync Cron Job - Delta sync products to Upstash VectorDB.

Runs WEEKLY (Sundays 3:00 AM UTC) as a fallback to catch missed webhooks.
Primary sync is handled via Shopify webhooks (products/create, products/update, products/delete).

Architecture (post 7-Jun OOM):
- The cron is a thin PRODUCER on the webhook tier. It resolves the Shopify
  clients and fans out one Dramatiq message per client onto the
  ``cron.product.sync`` lane (JOB_PRODUCT_DELTA_SYNC). The heavy per-client
  ingestion runs on the ``queue-background-worker`` service — never inline on
  the 512 MiB webhook-handlers instances that OOM'd.
- If the lane is disabled (``WEBHOOK_QUEUE_LANES`` lacks ``product_sync``) or the
  broker is unreachable, the cron SKIPS and logs rather than running the heavy
  sweep inline on the web tier.
- Per-client work is itself memory-batched in the orchestrator
  (``PRODUCT_DELTA_SYNC_BATCH_SIZE``).

To activate the offload in an environment:
  1. Add ``product_sync`` to ``WEBHOOK_QUEUE_LANES`` (with ``WEBHOOK_QUEUE_ENABLED=true``).
  2. Run a worker consuming the lane, e.g.::
       dramatiq fashion_bot.workers.run --queues cron.product.sync --threads 1

Features:
- Delta sync: Only updates changed products (based on content hash)
- Lookback period: Fetches products updated in last 7 days (168 hours)
- Multi-client support: Syncs all configured clients
- Efficient: Skips unchanged products, only upserts modified ones
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any

from fashion_bot.monitoring.otel_metrics import track_cron
from fashion_bot.trace_context import generate_trace_id
from fashion_bot.cron_jobs.cron_lock import (
    acquire_cron_lock,
    release_cron_lock,
    get_cron_lock_owner,
)

logger = logging.getLogger(__name__)


from fashion_bot.database_manager import (
    awith_retry,
    get_async_postgres_connection,
    is_connection_error,
)


# Redis distributed-lock settings so only one pod runs the weekly sync.
# Matches the pattern used by conversation_analytics_job.
LOCK_KEY = "product_vector_sync_lock"
LOCK_TTL_SECONDS = 7200  # 2 hours — covers a sequential sweep across all clients


@awith_retry
async def get_all_client_ids_with_shopify() -> List[str]:
    """
    Fetch all client IDs that have Shopify configuration.

    Returns:
        List of client IDs with active Shopify integrations
    """
    try:
        # NOTE: the canonical key in client_configs is 'shopify_details'
        # (see config_manager.aget_shopify_config). The previous value
        # 'shopify_config' matched nothing, which is why this query
        # silently returned 0 clients and the weekly sync became a no-op.
        query = """
            SELECT DISTINCT c.id
            FROM clients c
            INNER JOIN client_configs cc ON c.id = cc.client_id
            WHERE cc.config_key = 'shopify_details'
            AND cc.config_value IS NOT NULL
            AND cc.config_value::text != '{}'
            AND c.id IS NOT NULL
        """

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query)
                rows = await cur.fetchall()
                client_ids = [
                    str(row["id"] if isinstance(row, dict) else row[0])
                    for row in rows
                    if (row["id"] if isinstance(row, dict) else row[0])
                ]

        logger.info(f"📋 Found {len(client_ids)} clients with Shopify config")
        return client_ids

    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(f"❌ Error fetching client IDs: {e}")
        return []


# Default lookback period for weekly sync: 7 days = 168 hours
DEFAULT_SYNC_HOURS = 168


async def sync_client_products(client_id: str, hours: int = DEFAULT_SYNC_HOURS) -> Dict[str, Any]:
    """
    Perform delta sync for a single client.
    
    Args:
        client_id: The client ID to sync
        hours: Number of hours to look back for updated products (default: 168 = 7 days)
        
    Returns:
        Dictionary with sync result
    """
    try:
        from fashion_bot.services.product_ingestion import ProductIngestionOrchestrator
        
        orchestrator = ProductIngestionOrchestrator()
        result = await orchestrator.delta_sync_products(client_id=client_id, source="auto", hours=hours)
        
        return {
            "client_id": client_id,
            "success": result.success,
            "products_added": result.products_added,
            "products_updated": result.products_updated,
            "products_deleted": result.products_deleted,
            "products_unchanged": result.products_unchanged,
            "failed_count": result.failed_count,
            "duration": result.duration
        }
        
    except Exception as e:
        logger.error(f"❌ Delta sync failed for client {client_id}: {e}")
        return {
            "client_id": client_id,
            "success": False,
            "error": str(e)
        }


async def _enqueue_client_delta_sync(client_id: str, hours: int = DEFAULT_SYNC_HOURS) -> Dict[str, Any]:
    """Enqueue ONE client's delta sync onto the worker lane.

    Heavy ingestion must never run inline on the webhook tier (the 7-Jun OOM).
    ``submit_or_inline`` routes the job to the ``cron.product.sync`` lane when it
    is enabled; otherwise (lane off / broker unreachable) the ``inline`` callable
    here just SKIPS and logs — it deliberately does NOT run the sweep on this
    service.
    """
    from fashion_bot.workers import config as queue_config
    from fashion_bot.workers.enqueue import submit_or_inline

    async def _skip() -> Dict[str, Any]:
        logger.warning(
            "[PRODUCT_VECTOR_SYNC] queue lane disabled or broker unavailable — "
            "skipping client %s (heavy sync is never run inline on the webhook tier)",
            client_id,
        )
        return {"client_id": client_id, "queued": False, "skipped": True, "reason": "queue_unavailable"}

    payload = {"client_id": client_id, "hours": hours, "trace_id": generate_trace_id()}
    return await submit_or_inline(queue_config.JOB_PRODUCT_DELTA_SYNC, payload, _skip)


async def _async_product_vector_delta_sync() -> Dict[str, Any]:
    """
    Producer fan-out: enqueue one delta-sync job per Shopify client onto the
    ``queue-background-worker``. Returns a dispatch summary (NOT per-client
    product counts — those are produced on the worker).
    """
    start_time = datetime.now(timezone.utc)

    logger.info("=" * 60)
    logger.info(f"[PRODUCT_VECTOR_SYNC] 🚀 Dispatching delta sync at {start_time.isoformat()}")
    logger.info("=" * 60)

    # Get all clients with Shopify config
    client_ids = await get_all_client_ids_with_shopify()

    if not client_ids:
        logger.info("[PRODUCT_VECTOR_SYNC] No clients to sync")
        return {
            "success": True,
            "clients_processed": 0,
            "clients_queued": 0,
            "clients_skipped": 0,
            "message": "No clients with Shopify config found",
        }

    queued = 0
    skipped = 0
    for client_id in client_ids:
        result = await _enqueue_client_delta_sync(client_id)
        if result.get("queued"):
            queued += 1
            logger.info(f"[PRODUCT_VECTOR_SYNC] ⏩ Queued delta sync for client {client_id}")
        else:
            skipped += 1

    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()

    summary = {
        # Dispatch always "succeeds"; per-client sync success is tracked on the
        # worker (Dramatiq retries + product_sync_logs rows).
        "success": True,
        "clients_processed": len(client_ids),
        "clients_queued": queued,
        "clients_skipped": skipped,
        "duration_seconds": duration,
        "started_at": start_time.isoformat(),
        "completed_at": end_time.isoformat(),
    }

    logger.info("=" * 60)
    logger.info(f"[PRODUCT_VECTOR_SYNC] ✅ Dispatch complete in {duration:.2f}s: "
                f"{queued} queued, {skipped} skipped (of {len(client_ids)} clients)")
    if skipped:
        logger.warning(
            "[PRODUCT_VECTOR_SYNC] %d client(s) skipped — enable the 'product_sync' lane "
            "(WEBHOOK_QUEUE_LANES) and run a 'cron.product.sync' worker to process them",
            skipped,
        )
    logger.info("=" * 60)

    return summary


@track_cron(
    "product_sync",
    items_key=["clients_queued"],
)
async def product_vector_delta_sync() -> Dict[str, Any]:
    """
    Main entry point for the cron job — native async, invoked by
    AsyncIOScheduler on the app event loop.

    Runs delta sync for all configured clients.

    Acquires a Redis lock (LOCK_KEY) before running so that when the
    scheduler fires on multiple pods, only the pod that wins SETNX
    actually executes the sync. Other pods log a skip and return.
    """
    lease = None
    try:
        lease = await acquire_cron_lock(lock_key=LOCK_KEY, ttl_seconds=LOCK_TTL_SECONDS)
        if not lease:
            current_owner = await get_cron_lock_owner(LOCK_KEY)
            logger.info(
                "[PRODUCT_VECTOR_SYNC] Sync already running on another pod or lock backend unavailable. owner=%s",
                current_owner or "unknown",
            )
            return {
                "success": True,
                "skipped": True,
                "clients_processed": 0,
                "message": "Skipped: sync already running on another pod",
                "lock_owner": current_owner,
            }

        logger.info("[PRODUCT_VECTOR_SYNC] Lock acquired by %s", lease.owner_id)
        return await _async_product_vector_delta_sync()
    except Exception as e:
        logger.error(f"[PRODUCT_VECTOR_SYNC] ❌ Job failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
    finally:
        if lease:
            try:
                await release_cron_lock(lease)
            except Exception as exc:
                logger.warning("[PRODUCT_VECTOR_SYNC] Failed to release lock: %s", exc)


# NOTE: ``items_key`` is intentionally omitted — the ``product_delta_sync``
# actor already emits ``cron_items_processed`` for this work, and setting it
# here would double-count every synced product.
@track_cron("product_sync")
async def delta_sync_single_client(client_id: str, hours: int = DEFAULT_SYNC_HOURS) -> Dict[str, Any]:
    """
    Trigger delta sync for a single client.

    Called by the ``product_delta_sync`` Dramatiq actor (per-client fan-out of
    the weekly cron) and for manual/admin triggers.

    Tracked as ``job_name="product_sync"`` because this is where the sync can
    actually fail. The producer above is also tracked under that name, but it
    only dispatches and returns ``{"success": True}`` unconditionally — so
    before this decorator existed, ``cron_runs_total{job_name="product_sync",
    status="failure"}`` was never emitted by anything. Both the "Product Vector
    Sync Failed" and "Cron Job Failure Detected" alerts key off exactly that
    series, which left every per-client failure silent: the worker emitted
    ``cron_items_processed`` and nothing else.

    Consequence to be aware of: a weekly run now records one "run" per client
    rather than one per dispatch, so success counts on the Cron Job Monitoring
    dashboard scale with client count. That is the intended trade — a run count
    that tracks real work, and failures that are visible at all.

    Args:
        client_id: The client ID to sync
        hours: Lookback window in hours (default: 168 = 7 days)

    Returns:
        Sync result for the client
    """
    logger.info(f"[PRODUCT_VECTOR_SYNC] 🔄 Delta sync for client: {client_id} (lookback {hours}h)")
    return await sync_client_products(client_id, hours=hours)
