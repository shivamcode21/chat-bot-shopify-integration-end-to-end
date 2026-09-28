"""
Session Count Sync Cron Job — fetch daily unique session counts and order
counts from Shopify ShopifyQL via the GraphQL Admin API and persist to the
client_sessions table.

Runs DAILY. For each Shopify-backed client it:

1. Fetches Shopify credentials from client_configs (shopify_details key).
2. Queries yesterday's unique_sessions and orders via ShopifyQL (API ≥ 2025-10).
3. Upserts the result into client_sessions (idempotent on client_id + session_date).

Scheduling/placement: registered on the ecommagents-webhook-handlers Render
service via scheduler.py. A Redis lease ensures a single pod executes even
when multiple replicas are running.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict

from fashion_bot.config_manager import aget_shopify_config
from fashion_bot.cron_jobs.cron_lock import (
    acquire_cron_lock,
    get_cron_lock_owner,
    release_cron_lock,
)
from fashion_bot.cron_jobs.product_vector_sync_job import get_all_client_ids_with_shopify
from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.monitoring.otel_metrics import track_cron
from fashion_bot.trace_context import generate_trace_id, set_trace_id
from fashion_bot.utils.client_id_utils import is_client_blocklisted
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.shopify_throttle import shopify_graphql_post

logger = logging.getLogger(__name__)

LOCK_KEY = "session_count_sync_lock"
LOCK_TTL_SECONDS = 3600  # 1 hour — covers a sequential sweep across all clients

SHOPIFYQL_API_VERSION = "2025-10"

_SESSIONS_QUERY_TEMPLATE = "FROM sessions SHOW sessions SINCE {since} UNTIL {until}"
_ORDERS_QUERY_TEMPLATE = "FROM sales SHOW orders SINCE {since} UNTIL {until}"

_GRAPHQL_QUERY = """
    query ShopifyQL($query: String!) {
        shopifyqlQuery(query: $query) {
            tableData {
                columns { name dataType }
                rows
            }
            parseErrors
        }
    }
"""


def _extract_single_metric(response_data: Dict[str, Any], metric_name: str) -> int:
    """Extract a single numeric metric from a ShopifyQL tableData response."""
    shopifyql_result = response_data.get("data", {}).get("shopifyqlQuery", {})

    parse_errors = shopifyql_result.get("parseErrors")
    if parse_errors:
        logger.warning("🔍 ShopifyQL parse errors for %s: %s", metric_name, parse_errors)
        return 0

    table_data = shopifyql_result.get("tableData")
    if not table_data:
        return 0

    rows = table_data.get("rows") or []
    if not rows:
        return 0

    first_row = rows[0]
    if isinstance(first_row, dict):
        val = first_row.get(metric_name, 0)
    elif isinstance(first_row, list):
        val = first_row[-1] if first_row else 0
    else:
        return 0

    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


async def _fetch_metrics_for_client(
    client_id: str, shop_url: str, access_token: str, query_date: date
) -> Dict[str, int]:
    """Query ShopifyQL for a single client's sessions + orders on query_date."""
    graphql_url = f"https://{shop_url}/admin/api/{SHOPIFYQL_API_VERSION}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    since = query_date.isoformat()
    until = (query_date + timedelta(days=1)).isoformat()

    http_client = await get_shared_async_http_client()

    sessions_payload = {
        "query": _GRAPHQL_QUERY,
        "variables": {"query": _SESSIONS_QUERY_TEMPLATE.format(since=since, until=until)},
    }
    sessions_resp = await shopify_graphql_post(
        http_client, graphql_url, headers, sessions_payload, rate_limit_key=shop_url,
    )
    total_sessions = _extract_single_metric(sessions_resp, "sessions")

    orders_payload = {
        "query": _GRAPHQL_QUERY,
        "variables": {"query": _ORDERS_QUERY_TEMPLATE.format(since=since, until=until)},
    }
    orders_resp = await shopify_graphql_post(
        http_client, graphql_url, headers, orders_payload, rate_limit_key=shop_url,
    )
    total_orders = _extract_single_metric(orders_resp, "orders")

    return {"total_sessions": total_sessions, "total_orders": total_orders}


async def _upsert_session_count(
    client_id: str,
    shop_domain: str,
    session_date: date,
    total_sessions: int,
    total_orders: int,
) -> None:
    """Upsert a row into client_sessions (idempotent on client_id + session_date)."""
    query = """
        INSERT INTO client_sessions (client_id, shop_domain, session_date, total_sessions, total_orders, fetched_at)
        VALUES (%s::uuid, %s, %s, %s, %s, NOW())
        ON CONFLICT (client_id, session_date) DO UPDATE
            SET total_sessions = EXCLUDED.total_sessions,
                total_orders = EXCLUDED.total_orders,
                shop_domain = EXCLUDED.shop_domain,
                fetched_at = NOW()
    """
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                query, (client_id, shop_domain, session_date, total_sessions, total_orders)
            )


async def _sync_sessions() -> Dict[str, Any]:
    """Core logic: iterate all Shopify clients, fetch yesterday's sessions, persist."""
    query_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    logger.info(
        "🔍 [SESSION_SYNC] Starting session count sync for date=%s", query_date
    )

    client_ids = await get_all_client_ids_with_shopify()
    if not client_ids:
        logger.info("🔍 [SESSION_SYNC] No Shopify-backed clients found")
        return {"success": True, "clients_processed": 0, "date": str(query_date)}

    processed = 0
    skipped = 0
    errors = 0

    for client_id in client_ids:
        if is_client_blocklisted(client_id):
            skipped += 1
            continue

        try:
            config = await aget_shopify_config(client_id=client_id)
            shop_url = config.get("shop_url", "")
            access_token = config.get("access_token", "")

            if not shop_url or not access_token:
                logger.warning(
                    "⚠️ [SESSION_SYNC] Missing Shopify creds for client=%s", client_id
                )
                skipped += 1
                continue

            metrics = await _fetch_metrics_for_client(
                client_id, shop_url, access_token, query_date
            )

            await _upsert_session_count(
                client_id,
                shop_url,
                query_date,
                metrics["total_sessions"],
                metrics["total_orders"],
            )

            logger.info(
                "📦 [SESSION_SYNC] client=%s shop=%s date=%s sessions=%d orders=%d",
                client_id,
                shop_url,
                query_date,
                metrics["total_sessions"],
                metrics["total_orders"],
            )
            processed += 1

        except Exception as e:
            logger.error(
                "❌ [SESSION_SYNC] Failed for client=%s: %s",
                client_id,
                e,
                exc_info=True,
            )
            errors += 1

    logger.info(
        "✅ [SESSION_SYNC] Done. processed=%d skipped=%d errors=%d date=%s",
        processed,
        skipped,
        errors,
        query_date,
    )
    return {
        "success": errors == 0,
        "clients_processed": processed,
        "clients_skipped": skipped,
        "clients_errored": errors,
        "date": str(query_date),
    }


@track_cron("session_count_sync", items_key="clients_processed")
async def session_count_sync_daily() -> Dict[str, Any]:
    """Cron entry point — acquires Redis lease for single-pod execution."""
    set_trace_id(generate_trace_id())

    lease = None
    try:
        lease = await acquire_cron_lock(lock_key=LOCK_KEY, ttl_seconds=LOCK_TTL_SECONDS)
        if not lease:
            current_owner = await get_cron_lock_owner(LOCK_KEY)
            logger.info(
                "[SESSION_SYNC] Already running on another pod or lock backend "
                "unavailable. owner=%s",
                current_owner or "unknown",
            )
            return {
                "success": True,
                "skipped": True,
                "clients_processed": 0,
                "message": "Skipped: sync already running on another pod",
                "lock_owner": current_owner,
            }

        logger.info("[SESSION_SYNC] 🚀 Lock acquired by %s", lease.owner_id)
        return await _sync_sessions()
    except Exception as e:
        logger.error("[SESSION_SYNC] ❌ Job failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}
    finally:
        if lease:
            try:
                await release_cron_lock(lease)
            except Exception as exc:
                logger.warning("[SESSION_SYNC] Failed to release lock: %s", exc)
