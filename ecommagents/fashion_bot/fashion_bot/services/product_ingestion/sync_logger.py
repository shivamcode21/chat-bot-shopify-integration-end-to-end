"""
Product Sync Logger - Logs product sync operations to PostgreSQL (async only).

Tracks webhook and cron job sync activities for monitoring and debugging.
Also provides IngestionStatusTracker for progressive, polled status updates
during long-running ingestion/sync operations (mirrors OnboardingStatusTracker).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from decimal import Decimal

from fashion_bot.database_manager import get_async_postgres_connection

logger = logging.getLogger(__name__)


class ProductSyncLogger:
    """
    Logger for product sync operations.

    Tracks all product ingestion, update, and deletion operations
    from webhooks, cron jobs, and manual API calls.
    """

    # Sync source constants
    SOURCE_WEBHOOK = "webhook"
    SOURCE_CRON = "cron"
    SOURCE_MANUAL_API = "manual_api"

    # Status constants
    STATUS_STARTED = "started"
    STATUS_SUCCESS = "success"
    STATUS_PARTIAL_FAILURE = "partial_failure"
    STATUS_FAILURE = "failure"

    @staticmethod
    async def _ainsert_log(
        client_id: str,
        sync_source: str,
        sync_type: str,
        status: str,
        products_added: int = 0,
        products_updated: int = 0,
        products_deleted: int = 0,
        products_unchanged: int = 0,
        products_failed: int = 0,
        product_id: Optional[str] = None,
        product_title: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        trace_id: Optional[str] = None,
        shopify_webhook_id: Optional[str] = None,
        shopify_shop_domain: Optional[str] = None
    ) -> Optional[int]:
        """Insert a log entry into the database (async)."""
        try:
            query = """
                INSERT INTO product_sync_logs (
                    client_id, sync_source, sync_type, status,
                    products_added, products_updated, products_deleted,
                    products_unchanged, products_failed,
                    product_id, product_title, error_message,
                    duration_seconds, trace_id,
                    shopify_webhook_id, shopify_shop_domain
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
            """

            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query, (
                        client_id, sync_source, sync_type, status,
                        products_added, products_updated, products_deleted,
                        products_unchanged, products_failed,
                        product_id,
                        product_title[:500] if product_title else None,
                        error_message,
                        Decimal(str(duration_seconds)) if duration_seconds else None,
                        trace_id, shopify_webhook_id, shopify_shop_domain
                    ))
                    result = await cur.fetchone()

                    log_id = result["id"] if result else None
                    logger.debug(f"[SYNC_LOG] Logged sync event: id={log_id}, source={sync_source}, type={sync_type}")
                    return log_id

        except Exception as e:
            logger.error(f"[SYNC_LOG] Failed to log sync event: {e}")
            return None

    @staticmethod
    async def alog_webhook_event(
        client_id: str,
        sync_type: str,
        status: str,
        product_id: Optional[str] = None,
        product_title: Optional[str] = None,
        products_added: int = 0,
        products_updated: int = 0,
        products_deleted: int = 0,
        products_failed: int = 0,
        error_message: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        trace_id: Optional[str] = None,
        shopify_webhook_id: Optional[str] = None,
        shopify_shop_domain: Optional[str] = None
    ) -> Optional[int]:
        """Log a webhook sync event."""
        return await ProductSyncLogger._ainsert_log(
            client_id=client_id,
            sync_source=ProductSyncLogger.SOURCE_WEBHOOK,
            sync_type=sync_type, status=status,
            product_id=product_id, product_title=product_title,
            products_added=products_added, products_updated=products_updated,
            products_deleted=products_deleted, products_unchanged=0,
            products_failed=products_failed, error_message=error_message,
            duration_seconds=duration_seconds, trace_id=trace_id,
            shopify_webhook_id=shopify_webhook_id,
            shopify_shop_domain=shopify_shop_domain
        )

    @staticmethod
    async def alog_cron_event(
        client_id: str,
        sync_type: str,
        status: str,
        products_added: int = 0,
        products_updated: int = 0,
        products_deleted: int = 0,
        products_unchanged: int = 0,
        products_failed: int = 0,
        error_message: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        trace_id: Optional[str] = None
    ) -> Optional[int]:
        """Log a cron job sync event."""
        return await ProductSyncLogger._ainsert_log(
            client_id=client_id,
            sync_source=ProductSyncLogger.SOURCE_CRON,
            sync_type=sync_type, status=status,
            products_added=products_added, products_updated=products_updated,
            products_deleted=products_deleted, products_unchanged=products_unchanged,
            products_failed=products_failed, error_message=error_message,
            duration_seconds=duration_seconds, trace_id=trace_id
        )

    @staticmethod
    async def alog_manual_api_event(
        client_id: str,
        sync_type: str,
        status: str,
        products_added: int = 0,
        products_updated: int = 0,
        products_deleted: int = 0,
        products_unchanged: int = 0,
        products_failed: int = 0,
        product_id: Optional[str] = None,
        product_title: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        trace_id: Optional[str] = None,
    ) -> Optional[int]:
        """Log a manual API sync event.

        ``product_id`` / ``product_title`` are populated for single-product
        endpoints (e.g. ``/api/v1/products/ingest/single``). They map to the
        same columns used by webhook events.
        """
        return await ProductSyncLogger._ainsert_log(
            client_id=client_id,
            sync_source=ProductSyncLogger.SOURCE_MANUAL_API,
            sync_type=sync_type, status=status,
            products_added=products_added, products_updated=products_updated,
            products_deleted=products_deleted, products_unchanged=products_unchanged,
            products_failed=products_failed,
            product_id=product_id, product_title=product_title,
            error_message=error_message,
            duration_seconds=duration_seconds, trace_id=trace_id,
        )

    @staticmethod
    async def aget_sync_logs(
        client_id: str,
        limit: int = 50,
        offset: int = 0,
        sync_source: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Paginated listing of sync log entries for a client."""
        try:
            where = "WHERE client_id = %s"
            params: list = [client_id]

            if sync_source:
                where += " AND sync_source = %s"
                params.append(sync_source)
            if status:
                where += " AND status = %s"
                params.append(status)

            count_q = f"SELECT COUNT(*) FROM product_sync_logs {where}"
            data_q = f"""
                SELECT id, client_id, sync_source, sync_type, status,
                       products_added, products_updated, products_deleted,
                       products_unchanged, products_failed,
                       product_id, product_title, error_message,
                       duration_seconds, trace_id,
                       shopify_webhook_id, shopify_shop_domain,
                       created_at
                FROM product_sync_logs
                {where}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """

            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(count_q, params)
                    count_row = await cur.fetchone()
                    total = count_row["count"] if count_row else 0

                    await cur.execute(data_q, params + [limit, offset])
                    rows = await cur.fetchall()

                    items = []
                    for row in rows:
                        d = dict(row)
                        if d.get("created_at"):
                            d["created_at"] = d["created_at"].isoformat()
                        if d.get("duration_seconds") is not None:
                            d["duration_seconds"] = float(d["duration_seconds"])
                        items.append(d)

                    return {
                        "total": total,
                        "items": items,
                        "limit": limit,
                        "offset": offset,
                    }

        except Exception as e:
            logger.error(f"[SYNC_LOG] Failed to get sync logs: {e}")
            return {"total": 0, "items": [], "limit": limit, "offset": offset}

    @staticmethod
    async def aget_sync_stats(client_id: str, days: int = 7) -> Dict[str, Any]:
        """Get sync statistics for a client."""
        try:
            query = """
                SELECT
                    sync_source,
                    COUNT(*) as total_operations,
                    SUM(products_added) as total_added,
                    SUM(products_updated) as total_updated,
                    SUM(products_deleted) as total_deleted,
                    SUM(products_failed) as total_failed,
                    COUNT(CASE WHEN status = 'success' THEN 1 END) as successful_ops,
                    COUNT(CASE WHEN status = 'failure' THEN 1 END) as failed_ops
                FROM product_sync_logs
                WHERE client_id = %s
                AND created_at >= NOW() - INTERVAL '%s days'
                GROUP BY sync_source
            """

            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(query, (client_id, days))
                    rows = await cur.fetchall()

                    stats = {
                        "client_id": client_id,
                        "period_days": days,
                        "by_source": {}
                    }

                    for row in rows:
                        stats["by_source"][row["sync_source"]] = {
                            "total_operations": row["total_operations"],
                            "total_added": row["total_added"] or 0,
                            "total_updated": row["total_updated"] or 0,
                            "total_deleted": row["total_deleted"] or 0,
                            "total_failed": row["total_failed"] or 0,
                            "successful_ops": row["successful_ops"] or 0,
                            "failed_ops": row["failed_ops"] or 0,
                        }

                    return stats

        except Exception as e:
            logger.error(f"[SYNC_LOG] Failed to get sync stats: {e}")
            return {"error": str(e)}

    @staticmethod
    async def aget_latest_sync_event(
        client_id: str,
        sync_type: str,
        trace_id: Optional[str] = None,
        sync_source: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch the most recent sync log row for a client/run."""
        try:
            query = """
                SELECT
                    id, client_id, sync_source, sync_type, status,
                    products_added, products_updated, products_deleted,
                    products_unchanged, products_failed,
                    error_message, duration_seconds, trace_id, created_at
                FROM product_sync_logs
                WHERE client_id = %s
                  AND sync_type = %s
                  AND (%s IS NULL OR trace_id = %s)
                  AND (%s IS NULL OR sync_source = %s)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            """

            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        query,
                        (
                            client_id, sync_type,
                            trace_id, trace_id,
                            sync_source, sync_source,
                        ),
                    )
                    row = await cur.fetchone()
                    if not row:
                        return None

                    duration = row.get("duration_seconds")
                    created_at = row.get("created_at")
                    return {
                        "id": row.get("id"),
                        "client_id": row.get("client_id"),
                        "sync_source": row.get("sync_source"),
                        "sync_type": row.get("sync_type"),
                        "status": row.get("status"),
                        "products_added": row.get("products_added") or 0,
                        "products_updated": row.get("products_updated") or 0,
                        "products_deleted": row.get("products_deleted") or 0,
                        "products_unchanged": row.get("products_unchanged") or 0,
                        "products_failed": row.get("products_failed") or 0,
                        "error_message": row.get("error_message"),
                        "duration_seconds": float(duration) if duration is not None else None,
                        "trace_id": row.get("trace_id"),
                        "created_at": created_at.isoformat() if created_at else None,
                    }

        except Exception as e:
            logger.error(f"[SYNC_LOG] Failed to fetch latest sync event: {e}")
            return None


# ─────────────── Ingestion stage definitions ─────────────────────────

FULL_INGESTION_STAGES = [
    "source_detection",
    "product_fetch",
    "llm_extraction",
    "attribute_persistence",
    "bestseller_tagging",
    "search_upsert",
    "redis_cache",
]

DELTA_SYNC_STAGES = [
    "source_detection",
    "product_fetch",
    "hash_comparison",
    "llm_extraction",
    "attribute_persistence",
    "bestseller_tagging",
    "search_upsert",
    "redis_cache",
]


def _derive_ingestion_status(steps: Dict[str, Any]) -> str:
    """Derive overall ingestion status from individual step statuses."""
    statuses = [s.get("status", "pending") for s in steps.values()]
    if any(s == "running" for s in statuses):
        return "in_progress"
    if all(s in ("success", "skipped", "success_with_warnings") for s in statuses):
        if any(s == "success_with_warnings" for s in statuses):
            return "completed_with_warnings"
        return "completed"
    if any(s in ("failure", "failed") for s in statuses):
        return "failed"
    if all(s == "pending" for s in statuses):
        return "pending"
    return "in_progress"


class IngestionStatusTracker:
    """
    Persists per-step ingestion progress to ``product_sync_logs``.

    Mirrors ``OnboardingStatusTracker`` from ``client_onboarding.py``:
    - ``acreate_run`` inserts an initial row with all stages ``"pending"``.
    - ``aupdate_step`` updates a single step in the JSONB ``steps`` column.
    - ``aupdate_counters`` flushes aggregate product counts.
    - ``acomplete`` / ``aderive_and_complete`` marks the row terminal.

    Every mutating method writes to PostgreSQL immediately so the poll
    endpoint always returns fresh data.
    """

    def __init__(self, run_id: str, client_id: str):
        self.run_id = run_id
        self.client_id = client_id

    async def acreate_run(
        self,
        *,
        sync_source: str = "manual_api",
        sync_type: str = "full_ingestion",
        stages: Optional[List[str]] = None,
    ) -> None:
        """Insert the initial product_sync_logs row with all stages pending."""
        stage_list = stages or FULL_INGESTION_STAGES
        initial_steps = {
            stage: {"status": "pending", "message": ""}
            for stage in stage_list
        }
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO product_sync_logs
                            (client_id, run_id, sync_source, sync_type,
                             status, steps)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            self.client_id,
                            self.run_id,
                            sync_source,
                            sync_type,
                            "in_progress",
                            json.dumps(initial_steps),
                        ),
                    )
        except Exception as e:
            logger.error(f"❌ Failed to create ingestion run row: {e}", exc_info=True)

    async def aupdate_step(
        self,
        step_name: str,
        status: str,
        message: str = "",
        **extra: Any,
    ) -> None:
        """Update a single step's status inside the JSONB ``steps`` column."""
        step_data: Dict[str, Any] = {"status": status, "message": message}
        now_iso = datetime.now(timezone.utc).isoformat()
        if status == "running":
            step_data["started_at"] = now_iso
        if status in ("success", "failure", "failed", "skipped",
                       "partial_failure", "success_with_warnings"):
            step_data["completed_at"] = now_iso
        step_data.update(extra)
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE product_sync_logs
                           SET steps = jsonb_set(
                                   COALESCE(steps, '{}'::jsonb),
                                   %s, %s::jsonb
                               ),
                               updated_at = NOW()
                         WHERE run_id = %s
                        """,
                        (
                            [step_name],
                            json.dumps(step_data),
                            self.run_id,
                        ),
                    )
        except Exception as e:
            logger.error(
                f"❌ Failed to update ingestion step {step_name}: {e}",
                exc_info=True,
            )

    async def aupdate_counters(
        self,
        *,
        products_added: int = 0,
        products_updated: int = 0,
        products_deleted: int = 0,
        products_unchanged: int = 0,
        products_failed: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        """Flush aggregate counters to the run row."""
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE product_sync_logs
                           SET products_added     = %s,
                               products_updated   = %s,
                               products_deleted   = %s,
                               products_unchanged = %s,
                               products_failed    = %s,
                               error_message      = COALESCE(%s, error_message),
                               updated_at         = NOW()
                         WHERE run_id = %s
                        """,
                        (
                            products_added,
                            products_updated,
                            products_deleted,
                            products_unchanged,
                            products_failed,
                            error_message,
                            self.run_id,
                        ),
                    )
        except Exception as e:
            logger.error(f"❌ Failed to update ingestion counters: {e}", exc_info=True)

    async def acomplete(self, overall_status: str, duration_seconds: float) -> None:
        """Mark the ingestion run as terminal."""
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE product_sync_logs
                           SET status           = %s,
                               completed_at     = NOW(),
                               duration_seconds = %s,
                               updated_at       = NOW()
                         WHERE run_id = %s
                        """,
                        (overall_status, round(duration_seconds, 2), self.run_id),
                    )
        except Exception as e:
            logger.error(f"❌ Failed to complete ingestion run: {e}", exc_info=True)

    async def aderive_and_complete(self, duration_seconds: float) -> None:
        """Read current steps, derive overall status, and mark complete."""
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT steps FROM product_sync_logs WHERE run_id = %s",
                        (self.run_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        return
                    steps = row["steps"] or {}
                    overall = _derive_ingestion_status(steps)
                    await cur.execute(
                        """
                        UPDATE product_sync_logs
                           SET status           = %s,
                               completed_at     = NOW(),
                               duration_seconds = %s,
                               updated_at       = NOW()
                         WHERE run_id = %s
                        """,
                        (overall, round(duration_seconds, 2), self.run_id),
                    )
        except Exception as e:
            logger.error(
                f"❌ Failed to derive+complete ingestion run: {e}",
                exc_info=True,
            )


async def get_ingestion_run(client_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single ingestion run by client_id + run_id for polling."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, run_id, client_id, sync_source, sync_type,
                           status, steps,
                           products_added, products_updated, products_deleted,
                           products_unchanged, products_failed,
                           error_message, duration_seconds,
                           trace_id, created_at, updated_at, completed_at
                      FROM product_sync_logs
                     WHERE client_id = %s AND run_id = %s
                    """,
                    (client_id, run_id),
                )
                row = await cur.fetchone()
                if not row:
                    return None

                steps = row["steps"] or {}
                all_stages = list(steps.keys())
                total_steps = len(all_stages)
                completed_steps = sum(
                    1 for s in steps.values()
                    if s.get("status") in (
                        "success", "skipped", "failure", "failed",
                        "partial_failure", "success_with_warnings",
                    )
                )
                current_step = None
                for stage in all_stages:
                    if steps.get(stage, {}).get("status") == "running":
                        current_step = stage
                        break

                duration = row.get("duration_seconds")
                created = row.get("created_at")
                updated = row.get("updated_at")
                completed = row.get("completed_at")

                return {
                    "success": True,
                    "client_id": str(row["client_id"]),
                    "run_id": row["run_id"],
                    "sync_source": row["sync_source"],
                    "sync_type": row["sync_type"],
                    "status": row["status"],
                    "progress": {
                        "total_steps": total_steps,
                        "completed_steps": completed_steps,
                        "current_step": current_step,
                    },
                    "steps": steps,
                    "products_added": row.get("products_added") or 0,
                    "products_updated": row.get("products_updated") or 0,
                    "products_deleted": row.get("products_deleted") or 0,
                    "products_unchanged": row.get("products_unchanged") or 0,
                    "products_failed": row.get("products_failed") or 0,
                    "error_message": row.get("error_message"),
                    "duration_seconds": float(duration) if duration is not None else None,
                    "started_at": created.isoformat() if created else None,
                    "updated_at": updated.isoformat() if updated else None,
                    "completed_at": completed.isoformat() if completed else None,
                }
    except Exception as e:
        logger.error(f"❌ Failed to fetch ingestion run: {e}", exc_info=True)
        return None


async def get_ingestion_history(
    client_id: str,
    limit: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    """Return paginated product sync run history for a client, newest first.

    Only returns rows that have a ``run_id`` (i.e. progressive-tracked runs).
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                count_q = """
                    SELECT COUNT(*)
                      FROM product_sync_logs
                     WHERE client_id = %s AND run_id IS NOT NULL
                """
                await cur.execute(count_q, (client_id,))
                row = await cur.fetchone()
                total = row["count"] if row else 0

                await cur.execute(
                    """
                    SELECT run_id, sync_source, sync_type, status,
                           products_added, products_updated, products_deleted,
                           products_unchanged, products_failed,
                           duration_seconds,
                           created_at, updated_at, completed_at
                      FROM product_sync_logs
                     WHERE client_id = %s AND run_id IS NOT NULL
                     ORDER BY created_at DESC
                     LIMIT %s OFFSET %s
                    """,
                    (client_id, limit, offset),
                )
                rows = await cur.fetchall()

                items = []
                for r in rows:
                    created = r.get("created_at")
                    updated = r.get("updated_at")
                    completed = r.get("completed_at")
                    duration = r.get("duration_seconds")
                    items.append({
                        "run_id": r["run_id"],
                        "sync_source": r["sync_source"],
                        "sync_type": r["sync_type"],
                        "status": r["status"],
                        "products_added": r.get("products_added") or 0,
                        "products_updated": r.get("products_updated") or 0,
                        "products_deleted": r.get("products_deleted") or 0,
                        "products_unchanged": r.get("products_unchanged") or 0,
                        "products_failed": r.get("products_failed") or 0,
                        "duration_seconds": float(duration) if duration is not None else None,
                        "started_at": created.isoformat() if created else None,
                        "updated_at": updated.isoformat() if updated else None,
                        "completed_at": completed.isoformat() if completed else None,
                    })

                return {
                    "total": total,
                    "items": items,
                    "limit": limit,
                    "offset": offset,
                }
    except Exception as e:
        logger.error(f"❌ Failed to fetch ingestion history: {e}", exc_info=True)
        return {"total": 0, "items": [], "limit": limit, "offset": offset}
