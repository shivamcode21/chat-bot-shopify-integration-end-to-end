"""
Bloomerce Edited Orders Sync — fetch Shopify orders tagged BLOOMERCE_EDITED,
parse the structured note appended by ``astamp_bloomerce_edited``, and persist
each edit event to the ``bloomerce_order_edits`` Postgres table.

Runs DAILY.  For every Shopify-backed client it:

1. Queries ``orders(query: "tag:BLOOMERCE_EDITED updated_at:>=<since>")``
   via the Shopify Admin GraphQL API.
2. Parses ``[Bloomerce Edit]`` blocks from each order's note.
3. Upserts rows into ``bloomerce_order_edits`` (idempotent on
   ``client_id + shopify_order_id + updated_at``).

Scheduling: registered in ``scheduler.py`` via ``_guarded``.
A Redis lease ensures single-pod execution across replicas.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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

LOCK_KEY = "bloomerce_edited_sync_lock"
LOCK_TTL_SECONDS = 3600

LOOKBACK_DAYS = 2

_ORDERS_BY_TAG_QUERY = """
query fetchEditedOrders($cursor: String, $queryFilter: String) {
    orders(first: 50, after: $cursor, query: $queryFilter) {
        pageInfo {
            hasNextPage
            endCursor
        }
        edges {
            node {
                id
                name
                note
                tags
                createdAt
                updatedAt
                displayFulfillmentStatus
                totalPriceSet {
                    shopMoney { amount currencyCode }
                }
                customer {
                    firstName
                    lastName
                }
                shippingAddress {
                    firstName
                    lastName
                }
            }
        }
    }
}
"""

_NOTE_HEADER_RE = re.compile(
    r"^\[Bloomerce Edit\]\s+(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+\w+)",
    re.MULTILINE,
)


def _parse_edit_blocks(note: str) -> List[Dict[str, str]]:
    """Extract all ``[Bloomerce Edit]`` blocks from an order note.

    Each block is a contiguous set of ``Key: value`` lines starting with the
    header.  Returns a list of dicts with lowercased keys.
    """
    if not note:
        return []

    blocks: List[Dict[str, str]] = []
    for match in _NOTE_HEADER_RE.finditer(note):
        block: Dict[str, str] = {"timestamp": match.group("timestamp")}
        pos = match.end()
        seen_kv = False
        for line in note[pos:].split("\n"):
            line = line.strip()
            if not line:
                if seen_kv:
                    break
                continue
            seen_kv = True
            if ":" in line:
                key, _, value = line.partition(":")
                block[key.strip().lower()] = value.strip()
        blocks.append(block)

    return blocks


async def _fetch_edited_orders_for_client(
    client_id: str,
    shop_url: str,
    access_token: str,
    since_iso: str,
    api_version: str,
) -> List[Dict[str, Any]]:
    """Paginate through all BLOOMERCE_EDITED orders updated since *since_iso*."""
    graphql_url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    query_filter = f"tag:BLOOMERCE_EDITED updated_at:>={since_iso}"

    http_client = await get_shared_async_http_client()
    orders: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    max_pages = 20

    for _ in range(max_pages):
        payload = {
            "query": _ORDERS_BY_TAG_QUERY,
            "variables": {"queryFilter": query_filter, "cursor": cursor},
        }
        resp = await shopify_graphql_post(
            http_client, graphql_url, headers, payload, rate_limit_key=shop_url,
        )

        data = (resp.get("data") or {}).get("orders") or {}
        edges = data.get("edges") or []
        for edge in edges:
            node = edge.get("node")
            if node:
                orders.append(node)

        page_info = data.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return orders


def _gql_id_to_numeric(gql_id: str) -> str:
    """``gid://shopify/Order/123456`` → ``123456``."""
    return gql_id.rsplit("/", 1)[-1] if "/" in gql_id else gql_id


async def _upsert_edit_row(
    client_id: str,
    shopify_order_id: str,
    order_name: str,
    customer_name: str,
    order_status: str,
    update_type: str,
    updated_at: datetime,
    conversation_id: str,
    phone_number: str,
    session_id: str,
    order_value: str,
    shopify_created_at: Optional[datetime],
) -> None:
    query = """
        INSERT INTO bloomerce_order_edits (
            client_id, shopify_order_id, order_name, customer_name,
            order_status, update_type, updated_at, conversation_id,
            phone_number, session_id, order_value, shopify_created_at, fetched_at
        )
        VALUES (
            %s::uuid, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s, NOW()
        )
        ON CONFLICT (client_id, shopify_order_id, updated_at) DO UPDATE
            SET order_name       = EXCLUDED.order_name,
                customer_name    = EXCLUDED.customer_name,
                order_status     = EXCLUDED.order_status,
                update_type      = EXCLUDED.update_type,
                conversation_id  = EXCLUDED.conversation_id,
                phone_number     = EXCLUDED.phone_number,
                session_id       = EXCLUDED.session_id,
                order_value      = EXCLUDED.order_value,
                shopify_created_at = EXCLUDED.shopify_created_at,
                fetched_at       = NOW()
    """
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (
                client_id, shopify_order_id, order_name, customer_name,
                order_status, update_type, updated_at, conversation_id,
                phone_number, session_id, order_value, shopify_created_at,
            ))


_IST_OFFSET = timezone(timedelta(hours=5, minutes=30))


def _parse_note_timestamp(raw: str) -> Optional[datetime]:
    """Parse ``2026-07-07 10:23 IST`` into a timezone-aware datetime."""
    cleaned = raw.replace("IST", "").strip()
    try:
        naive = datetime.strptime(cleaned, "%Y-%m-%d %H:%M")
        return naive.replace(tzinfo=_IST_OFFSET)
    except (ValueError, TypeError):
        return None


async def _process_client(client_id: str, since_iso: str) -> Dict[str, int]:
    config = await aget_shopify_config(client_id=client_id)
    shop_url = config.get("shop_url", "")
    access_token = config.get("access_token", "")
    api_version = config.get("api_version", "2024-04")

    if not shop_url or not access_token:
        logger.warning("⚠️ [EDITED_SYNC] Missing Shopify creds for client=%s", client_id)
        return {"orders": 0, "edits": 0, "skipped": True}

    orders = await _fetch_edited_orders_for_client(
        client_id, shop_url, access_token, since_iso, api_version,
    )

    edits_upserted = 0
    for order in orders:
        note = order.get("note") or ""
        blocks = _parse_edit_blocks(note)
        if not blocks:
            continue

        shopify_order_id = _gql_id_to_numeric(order.get("id", ""))
        order_name = order.get("name") or ""
        shopify_created_at: Optional[datetime] = None
        if order.get("createdAt"):
            try:
                shopify_created_at = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        shipping = order.get("shippingAddress") or {}
        customer = order.get("customer") or {}
        fallback_customer_name = (
            f"{shipping.get('firstName') or customer.get('firstName') or ''} "
            f"{shipping.get('lastName') or customer.get('lastName') or ''}"
        ).strip()

        fulfillment_status = (order.get("displayFulfillmentStatus") or "UNFULFILLED").lower()

        price_set = (order.get("totalPriceSet") or {}).get("shopMoney") or {}
        order_value = ""
        if price_set.get("amount"):
            currency = price_set.get("currencyCode", "INR")
            order_value = f"{price_set['amount']} {currency}"

        for block in blocks:
            updated_at = _parse_note_timestamp(block.get("timestamp", ""))
            if not updated_at:
                continue

            try:
                await _upsert_edit_row(
                    client_id=client_id,
                    shopify_order_id=shopify_order_id,
                    order_name=block.get("order") or order_name,
                    customer_name=block.get("customer") or fallback_customer_name,
                    order_status=block.get("status") or fulfillment_status,
                    update_type=block.get("updated") or "unknown",
                    updated_at=updated_at,
                    conversation_id=block.get("conversation") or "",
                    phone_number=block.get("phone") or "",
                    session_id=block.get("session") or "",
                    order_value=order_value,
                    shopify_created_at=shopify_created_at,
                )
                edits_upserted += 1
            except Exception as exc:
                logger.error(
                    "❌ [EDITED_SYNC] Upsert failed client=%s order=%s: %s",
                    client_id, shopify_order_id, exc, exc_info=True,
                )

    logger.info(
        "📦 [EDITED_SYNC] client=%s orders=%d edits_upserted=%d",
        client_id, len(orders), edits_upserted,
    )
    return {"orders": len(orders), "edits": edits_upserted}


async def _sync_edited_orders() -> Dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime(
        "%Y-%m-%dT00:00:00Z"
    )
    logger.info("🔍 [EDITED_SYNC] Starting sync since=%s", since)

    client_ids = await get_all_client_ids_with_shopify()
    if not client_ids:
        logger.info("🔍 [EDITED_SYNC] No Shopify-backed clients found")
        return {"success": True, "clients_processed": 0}

    processed = 0
    skipped = 0
    errors = 0
    total_edits = 0

    for client_id in client_ids:
        if is_client_blocklisted(client_id):
            skipped += 1
            continue
        try:
            result = await _process_client(client_id, since)
            if result.get("skipped"):
                skipped += 1
            else:
                processed += 1
                total_edits += result.get("edits", 0)
        except Exception as exc:
            logger.error(
                "❌ [EDITED_SYNC] Failed for client=%s: %s",
                client_id, exc, exc_info=True,
            )
            errors += 1

    logger.info(
        "✅ [EDITED_SYNC] Done. processed=%d skipped=%d errors=%d edits=%d",
        processed, skipped, errors, total_edits,
    )
    return {
        "success": errors == 0,
        "clients_processed": processed,
        "clients_skipped": skipped,
        "clients_errored": errors,
        "total_edits": total_edits,
    }


@track_cron("bloomerce_edited_sync", items_key="total_edits")
async def bloomerce_edited_sync_daily() -> Dict[str, Any]:
    """Cron entry point — acquires Redis lease for single-pod execution."""
    set_trace_id(generate_trace_id())

    lease = None
    try:
        lease = await acquire_cron_lock(lock_key=LOCK_KEY, ttl_seconds=LOCK_TTL_SECONDS)
        if not lease:
            current_owner = await get_cron_lock_owner(LOCK_KEY)
            logger.info(
                "[EDITED_SYNC] Already running on another pod or lock backend "
                "unavailable. owner=%s",
                current_owner or "unknown",
            )
            return {
                "success": True,
                "skipped": True,
                "total_edits": 0,
                "clients_processed": 0,
                "message": "Skipped: sync already running on another pod",
                "lock_owner": current_owner,
            }

        logger.info("[EDITED_SYNC] 🚀 Lock acquired by %s", lease.owner_id)
        return await _sync_edited_orders()
    except Exception as e:
        logger.error("[EDITED_SYNC] ❌ Job failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e), "total_edits": 0}
    finally:
        if lease:
            try:
                await release_cron_lock(lease)
            except Exception as exc:
                logger.warning("[EDITED_SYNC] Failed to release lock: %s", exc)
