"""
Shopify Product Webhook Handler - Handles product create/update/delete events.

Listens to:
- products/create: New product created in Shopify
- products/update: Existing product updated in Shopify  
- products/delete: Product deleted from Shopify

These webhooks keep the vector DB in sync with Shopify product catalog.
"""

import asyncio
import logging
import json
import time
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

from fastapi import APIRouter, Request

from fashion_bot.trace_context import generate_trace_id, set_trace_id
from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.utils.client_identity_cache import aget_client_id_by_shop_domain as _aget_cid_by_shop
from fashion_bot.rollbar_config import report_error
from fashion_bot.services.product_ingestion.sync_logger import ProductSyncLogger
from fashion_bot.monitoring.otel_metrics import set_request_client_id
from fashion_bot.env_loader import get_env, get_int, get_float
from fashion_bot.utils.client_id_utils import is_client_blocklisted, is_shop_domain_blocklisted
# Queue producer. Safe, dramatiq-free import: submit_or_inline only imports the
# broker/actors lazily when WEBHOOK_QUEUE_ENABLED routes the lane through the
# queue; with the flag off (default) it just awaits the inline closure, so the
# behaviour below is byte-for-byte the legacy path.
from fashion_bot.workers.enqueue import submit_or_inline
from fashion_bot.workers.config import (
    JOB_PRODUCT_UPSERT,
    JOB_PRODUCT_DELETE,
    JOB_INVENTORY_UPDATE,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Shopify Product Webhooks"])


def _shopify_unmapped_domain_allowlist() -> set:
    """Comma-separated env list of shop domains that are intentionally
    unmapped (legacy stores, demo / dev accounts). Webhooks from these
    shops are silently ignored. Anything not on the list triggers an
    error escalation so tenant-mapping bugs surface immediately.
    """
    raw = get_env("SHOPIFY_UNMAPPED_DOMAIN_ALLOWLIST", "") or ""
    return {d.strip().lower() for d in raw.split(",") if d.strip()}

# Fields populated exclusively by LLM extraction. When LLM is skipped
# (e.g. inventory-only updates) we carry these over from the existing
# Upstash Search document so they are not overwritten with empty defaults.
# Single source of truth lives in product_llm_cache — importing rather than
# mirroring keeps the preserve path and the cache write from silently drifting
# apart (AGENTS.md: Shared Utilities Over Duplication). Safe at module scope:
# product_llm_cache imports only stdlib, so there is no cycle.
from fashion_bot.services.product_ingestion.product_llm_cache import (  # noqa: E402
    CRON_OWNED_CONTENT_FIELDS as _CRON_OWNED_CONTENT_FIELDS,
    LLM_EXTRACTED_CONTENT_FIELDS as _LLM_EXTRACTED_CONTENT_FIELDS,
)

# Admin GraphQL enrich tuning. Shopify product webhooks omit metafields, so
# we re-fetch them via GraphQL. Under bulk-update bursts (e.g. price /
# discount / inventory edits firing dozens of products/update webhooks at
# once) the original unbounded, no-retry, 10s path saturated the shared
# httpx client and timed out — silently dropping every metafield-derived
# field (size_chart, care_instructions, seo, collections). These are tunable
# via env without a code change.
_ENRICH_TIMEOUT_SECONDS = get_float("WEBHOOK_ENRICH_TIMEOUT_SECONDS", 20.0)
_ENRICH_MAX_ATTEMPTS = get_int("WEBHOOK_ENRICH_MAX_ATTEMPTS", 3)
_ENRICH_MAX_CONCURRENCY = get_int("WEBHOOK_ENRICH_MAX_CONCURRENCY", 8)

# Bound concurrent enrich calls so a webhook burst can't exhaust the shared
# HTTP pool / event loop and cascade into timeouts. One shared event loop
# (AGENTS.md), so a module-level semaphore has a stable owner; the benign
# double-init race on lazy creation is acceptable.
_enrich_semaphore: Optional[asyncio.Semaphore] = None


def _hash_field_fingerprint(normalized_product) -> str:
    """Return a compact fingerprint of the fields that feed content_hash_no_inventory.

    Useful for diagnosing *why* the hash changed when comparing across webhook
    deliveries — log the fingerprint alongside the hash.
    """
    try:
        desc = normalized_product.description or ""
        desc_preview = desc[:80].replace("\n", " ") if desc else ""
        tags = sorted(normalized_product.tags)[:5] if normalized_product.tags else []
        return (
            f"status={normalized_product.status} "
            f"vendor={normalized_product.vendor} "
            f"type={normalized_product.product_type} "
            f"price={normalized_product.price_min}-{normalized_product.price_max} "
            f"compare_at={normalized_product.compare_at_price_min}-{normalized_product.compare_at_price_max} "
            f"disc={normalized_product.discount_pct} "
            f"colors={len(normalized_product.colors or [])} "
            f"sizes={len(normalized_product.sizes or [])} "
            f"fabric={'yes' if normalized_product.fabric else 'no'} "
            f"care={'yes' if normalized_product.care_instructions else 'no'} "
            f"img={'yes' if normalized_product.image_url else 'no'} "
            f"tags_sample={tags} "
            f"desc_len={len(desc)} desc_start='{desc_preview}'"
        )
    except Exception as exc:
        return f"fingerprint_error:{exc}"


def _get_enrich_semaphore() -> asyncio.Semaphore:
    global _enrich_semaphore
    if _enrich_semaphore is None:
        _enrich_semaphore = asyncio.Semaphore(_ENRICH_MAX_CONCURRENCY)
    return _enrich_semaphore


# Fields sourced from the Admin GraphQL enrich (collections + resolved
# metafields). When enrich fails we must NOT overwrite these with empty
# defaults — carry them over from the existing Upstash Search document so a
# transient failure doesn't wipe size charts, care instructions, SEO, etc.
_ENRICH_DERIVED_CONTENT_FIELDS = ["collections", "metafield_attributes"]
_ENRICH_DERIVED_METADATA_FIELDS = [
    "size_chart",
    "care_instructions",
    "all_metafields",
    "seo_title",
    "seo_description",
]


def _preserve_enrich_fields(
    search_doc: dict,
    trace_id: str,
    search_service,
    client_id: str,
    product_id: str,
) -> None:
    """Merge enrich-derived fields from the existing Upstash document into
    *search_doc* in-place. Called only when the Admin GraphQL enrich failed,
    so the freshly-normalized doc has empty collections/metafields. Restores a
    field only when the new value is empty and the existing value is present.

    Also restores content_hash / content_hash_no_inventory from the existing
    doc whenever any field actually got restored above. to_search_document()
    computed those hashes from the normalized product BEFORE this function
    ran -- with metafield_attributes empty, since that's exactly the enrich
    failure this function exists to paper over. Left alone, the stored doc
    would carry restored (correct) metafield-derived content next to a hash
    that says that content is absent, so the next delta sync sees a spurious
    mismatch and pays for a full LLM+OCR re-extraction that was never needed.
    Carrying the old hash over avoids that false mismatch for the common case
    (only the enrich-derived fields needed restoring); it can't detect a
    *different* real change to a non-enrich-derived field (price, tags, ...)
    landing in this same webhook, same known trade-off this function already
    accepts for every other field it restores here.
    """
    try:
        existing_doc = search_service.fetch_document_by_id(client_id, product_id)
        if not existing_doc:
            logger.info(
                f"[PRODUCT_WEBHOOK] [{trace_id}] ℹ️ Enrich failed and no existing "
                f"Upstash doc to preserve metafields from (product {product_id})"
            )
            return

        existing_content = existing_doc.get("content") or {}
        existing_metadata = existing_doc.get("metadata") or {}
        doc_content = search_doc.setdefault("content", {})
        doc_metadata = search_doc.setdefault("metadata", {})

        def _empty(v) -> bool:
            return v is None or v == "" or v == [] or v == {}

        preserved = []
        for f in _ENRICH_DERIVED_CONTENT_FIELDS:
            if _empty(doc_content.get(f)) and not _empty(existing_content.get(f)):
                doc_content[f] = existing_content[f]
                preserved.append(f)
        for f in _ENRICH_DERIVED_METADATA_FIELDS:
            if _empty(doc_metadata.get(f)) and not _empty(existing_metadata.get(f)):
                doc_metadata[f] = existing_metadata[f]
                preserved.append(f)

        if preserved:
            for hash_field in ("content_hash", "content_hash_no_inventory"):
                old_hash = existing_metadata.get(hash_field)
                if not _empty(old_hash):
                    doc_metadata[hash_field] = old_hash
            logger.info(
                f"[PRODUCT_WEBHOOK] [{trace_id}] 🛟 Enrich failed — preserved "
                f"metafield-derived fields from Upstash: {preserved} "
                f"(and their content_hash/content_hash_no_inventory)"
            )
    except Exception as e:
        logger.warning(
            f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Failed to preserve enrich fields: {repr(e)}"
        )


def _restore_if_blank(target: Dict[str, Any], key: str, old_val: Any) -> bool:
    """Fill *key* from a previously-stored value only when the fresh doc is blank.

    Shared by the LLM and cron-owned preserve paths so "blank" means the same
    thing in both. ``False`` and ``0`` count as blank on purpose: a webhook-built
    document carries ``bestseller=False`` and zeroed analytics, which is absence
    rather than a real value.
    """
    if not old_val:
        return False
    new_val = target.get(key)
    is_blank = (
        new_val is None
        or new_val == ""
        or new_val == []
        or new_val == 0
        or new_val is False
    )
    if not is_blank:
        return False
    target[key] = old_val
    return True


def _preserve_cron_owned_fields(
    search_doc: Dict[str, Any],
    trace_id: str,
    cached_entry: Optional[Dict[str, Any]] = None,
    search_service: Optional[Any] = None,
    client_id: Optional[str] = None,
    product_id: Optional[str] = None,
) -> None:
    """Carry the monthly cron's fields across a webhook-driven upsert.

    ``bestseller`` and the engagement analytics are written only by
    ``bestseller_refresh_job``; a Shopify webhook payload knows nothing about
    them, so the freshly-normalized document has ``bestseller=False`` and no
    analytics keys at all. Because Upstash Search upsert REPLACES the document,
    every product webhook was demoting bestsellers and deleting the analytics.

    Unlike ``_preserve_llm_fields`` this runs on **every** upsert, not only when
    extraction is skipped — a genuine title change must not demote a bestseller
    either.

    Staleness is handled at the source: ``aupdate_cached_cron_fields`` re-syncs
    the cache when the cron demotes a product, so a stale ``bestseller: true``
    cannot resurrect it here.

    Deliberately does NOT fall back to Upstash when a cache entry exists but
    carries no cron fields — that is the normal state for the vast majority of
    products, which are not bestsellers and have no analytics.
    ``fetch_document_by_id`` is synchronous and would block the event loop on
    essentially every product webhook, in the one path this Redis cache exists
    to keep read-free. The fallback is reserved for a genuinely cold entry.

    Consequence on first deploy: entries written before this change have no
    cron block, so a bestseller is not restored until ``bestseller_refresh``
    next re-syncs the cache. That matches today's behaviour (the flag is being
    wiped on every webhook already), so it delays the fix rather than
    regressing anything.
    """
    try:
        from fashion_bot.services.product_ingestion.product_llm_cache import (
            collect_cron_owned_fields_from_doc,
        )

        if cached_entry:
            values = cached_entry.get("cron_owned_fields")
            source = "Redis cache"
        elif search_service and client_id and product_id:
            existing_doc = search_service.fetch_document_by_id(client_id, product_id)
            values = collect_cron_owned_fields_from_doc(existing_doc or {})
            source = "Upstash Search fallback"
        else:
            values = None
            source = "unavailable"

        if not values:
            return

        doc_content = search_doc.get("content", {})
        preserved = [
            field for field in _CRON_OWNED_CONTENT_FIELDS
            if _restore_if_blank(doc_content, field, values.get(field))
        ]

        if preserved:
            logger.info(
                f"[PRODUCT_WEBHOOK] [{trace_id}] ✅ Preserved cron-owned fields "
                f"({source}): {preserved}"
            )
    except Exception as e:
        logger.warning(
            f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Failed to preserve cron-owned fields: {e}"
        )


def _preserve_llm_fields(
    search_doc: dict,
    trace_id: str,
    cached_llm_fields: dict = None,
    search_service=None,
    client_id: str = None,
    product_id: str = None,
) -> None:
    """Merge previously-extracted LLM fields into *search_doc* in-place.

    *cached_llm_fields* (from Redis) is the primary source.  If not
    available, falls back to fetching from Upstash Search (requires
    *search_service*, *client_id*, and *product_id*).
    """
    try:
        llm_values = cached_llm_fields
        source = "Redis cache"

        if not llm_values and search_service and client_id and product_id:
            existing_doc = search_service.fetch_document_by_id(client_id, product_id)
            if existing_doc:
                from fashion_bot.services.product_ingestion.product_llm_cache import (
                    collect_llm_fields_from_doc,
                )
                llm_values = collect_llm_fields_from_doc(existing_doc)
                source = "Upstash Search fallback"

        if not llm_values:
            logger.info(
                f"[PRODUCT_WEBHOOK] [{trace_id}] No cached LLM fields to preserve"
            )
            return

        doc_content = search_doc.get("content", {})
        preserved = []

        for field in _LLM_EXTRACTED_CONTENT_FIELDS:
            if _restore_if_blank(doc_content, field, llm_values.get(field)):
                preserved.append(field)

        if preserved:
            logger.info(
                f"[PRODUCT_WEBHOOK] [{trace_id}] ✅ Preserved LLM fields ({source}): {preserved}"
            )
    except Exception as e:
        logger.warning(
            f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Failed to preserve LLM fields: {e}",
            exc_info=True,
        )

_WEBHOOK_ENRICH_QUERY = """
query enrichProduct($id: ID!) {
    product(id: $id) {
        seo {
            title
            description
        }
        # Keep this cap in sync with the ingestion fetch
        # (shopify_product_service.py uses collections(first: 65)). The EOSS /
        # sale collection is ordered LAST by Shopify, so a smaller cap here drops
        # it on a product-update upsert (the doc is fully replaced, no merge),
        # silently removing the product from EOSS search results.
        collections(first: 65) {
            edges { node { title handle } }
        }
        metafields(first: 50) {
            edges {
                node {
                    namespace
                    key
                    value
                    type
                    reference {
                        ... on Metaobject { displayName handle type }
                        ... on TaxonomyValue { name }
                    }
                    references(first: 20) {
                        edges {
                            node {
                                ... on Metaobject { displayName handle type }
                                ... on TaxonomyValue { name }
                            }
                        }
                    }
                }
            }
        }
    }
}
"""


async def _enrich_webhook_with_graphql(
    graphql_format: dict,
    client_id: str,
    trace_id: str,
) -> Tuple[dict, bool]:
    """Fetch collections and resolved metafields via Admin GraphQL and merge
    into the converted webhook dict so the normalizer produces identical
    output to the full-sync path.

    Returns ``(graphql_format, enriched_ok)``. ``enriched_ok`` is True only
    when Shopify returned a product node — i.e. the merged collections /
    metafields are authoritative. On failure (timeout, transport error,
    non-200, cost-throttle) it is False, signalling the caller to preserve
    the prior Upstash values instead of overwriting them with empty defaults.

    Retries transient failures with exponential backoff and bounds concurrency
    via a shared semaphore so a burst of webhooks can't saturate the shared
    HTTP client (the original 10s, no-retry, unbounded path silently dropped
    metafields under load).
    """
    try:
        from fashion_bot.config_manager import aget_shopify_config
        from fashion_bot.utils.http_client import get_shared_async_http_client

        config = await aget_shopify_config(client_id=client_id)
        shop_url = config.get("shop_url") or config.get("shop_domain", "")
        token = config.get("access_token", "")
        api_ver = config.get("api_version", "2024-04")

        if not shop_url or not token:
            return graphql_format, False

        product_gid = graphql_format.get("id", "")
        http = await get_shared_async_http_client()
        semaphore = _get_enrich_semaphore()

        last_error = None
        for attempt in range(1, _ENRICH_MAX_ATTEMPTS + 1):
            try:
                async with semaphore:
                    resp = await http.post(
                        f"https://{shop_url}/admin/api/{api_ver}/graphql.json",
                        headers={
                            "X-Shopify-Access-Token": token,
                            "Content-Type": "application/json",
                        },
                        json={"query": _WEBHOOK_ENRICH_QUERY, "variables": {"id": product_gid}},
                        timeout=_ENRICH_TIMEOUT_SECONDS,
                    )

                if resp.status_code != 200:
                    last_error = f"HTTP {resp.status_code}"
                    # 429 / 5xx are transient — retry; other 4xx are not.
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < _ENRICH_MAX_ATTEMPTS:
                        await asyncio.sleep(min(2 ** (attempt - 1), 8))
                        continue
                    logger.warning(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Enrich GraphQL HTTP "
                        f"{resp.status_code} (attempt {attempt}/{_ENRICH_MAX_ATTEMPTS})"
                    )
                    return graphql_format, False

                payload = resp.json()
                # Shopify cost-throttling returns 200 with errors[].THROTTLED
                # and null data — treat as transient, not as "no metafields".
                errors = payload.get("errors") or []
                throttled = any(
                    isinstance(e, dict)
                    and (e.get("extensions") or {}).get("code") == "THROTTLED"
                    for e in errors
                )
                product_node = (payload.get("data") or {}).get("product")
                if throttled or product_node is None:
                    last_error = "THROTTLED" if throttled else (str(errors)[:200] or "no product node")
                    if attempt < _ENRICH_MAX_ATTEMPTS:
                        await asyncio.sleep(min(2 ** (attempt - 1), 8))
                        continue
                    logger.warning(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Enrich returned no product "
                        f"({last_error}) after {attempt} attempts"
                    )
                    return graphql_format, False

                seo = product_node.get("seo")
                if seo:
                    graphql_format["seo"] = seo

                collections = product_node.get("collections")
                if collections:
                    graphql_format["collections"] = collections

                metafields = product_node.get("metafields")
                if metafields:
                    graphql_format["metafields"] = metafields

                logger.info(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] ✅ Enriched webhook with GraphQL "
                    f"(seo={bool(seo)}, "
                    f"collections={len((collections or {}).get('edges', []))}, "
                    f"metafields={len((metafields or {}).get('edges', []))}, "
                    f"attempt={attempt})"
                )
                return graphql_format, True

            except Exception as e:
                # Transport / timeout errors. repr(e) so timeouts (whose str()
                # is empty) are no longer logged as a blank message.
                last_error = repr(e)
                if attempt < _ENRICH_MAX_ATTEMPTS:
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))
                    continue
                logger.warning(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Enrich failed (non-fatal) "
                    f"after {attempt} attempts: {last_error}"
                )
                return graphql_format, False

        return graphql_format, False

    except Exception as e:
        logger.warning(
            f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Enrich setup failed (non-fatal): {repr(e)}"
        )
        return graphql_format, False


async def aget_client_id_from_shop_domain(shop_domain: str) -> Optional[str]:
    """Resolve shop domain to client_id via unified tiered cache."""
    return await _aget_cid_by_shop(shop_domain)


async def handle_product_delete(
    product_id: str, 
    client_id: str, 
    trace_id: str,
    shopify_webhook_id: str = None,
    shopify_shop_domain: str = None
) -> Dict[str, Any]:
    """
    Handle product deletion - remove from vector DB.
    
    Args:
        product_id: Shopify product ID
        client_id: Client UUID
        trace_id: Request trace ID for logging
        shopify_webhook_id: Shopify webhook ID
        shopify_shop_domain: Shop domain
        
    Returns:
        Result dictionary with success status
    """
    start_time = time.time()
    try:
        from fashion_bot.services.product_ingestion.upstash_search_service import UpstashSearchService

        search_service = UpstashSearchService()
        doc_id = f"{client_id}_{product_id}"

        logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] 🗑️ Deleting product {product_id} from Search index")

        deleted_count = search_service.delete_documents(client_id, ids=[doc_id])

        duration = time.time() - start_time

        if deleted_count > 0:
            logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] ✅ Product {product_id} deleted from Search index")
            
            # Log to database
            await ProductSyncLogger.alog_webhook_event(
                client_id=client_id,
                sync_type="products/delete",
                status=ProductSyncLogger.STATUS_SUCCESS,
                product_id=product_id,
                products_deleted=deleted_count,
                duration_seconds=duration,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain
            )
            
            return {
                "success": True,
                "action": "deleted",
                "product_id": product_id,
                "deleted_count": deleted_count
            }
        else:
            logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] ℹ️ Product {product_id} not found in Search index (may not have been indexed)")
            
            # Log to database - still a success, product just wasn't in index
            await ProductSyncLogger.alog_webhook_event(
                client_id=client_id,
                sync_type="products/delete",
                status=ProductSyncLogger.STATUS_SUCCESS,
                product_id=product_id,
                products_deleted=0,
                duration_seconds=duration,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain
            )
            
            return {
                "success": True,
                "action": "not_found",
                "product_id": product_id,
                "message": "Product was not in vector index"
            }
            
    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"[PRODUCT_WEBHOOK] [{trace_id}] ❌ Failed to delete product {product_id}: {e}")
        
        # Report to Rollbar
        report_error(
            "Product webhook delete failed",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            client_id=client_id,
            product_id=product_id,
            trace_id=trace_id,
            shopify_webhook_id=shopify_webhook_id,
            shopify_shop_domain=shopify_shop_domain,
            action="delete",
        )
        
        # Log failure
        await ProductSyncLogger.alog_webhook_event(
            client_id=client_id,
            sync_type="products/delete",
            status=ProductSyncLogger.STATUS_FAILURE,
            product_id=product_id,
            products_failed=1,
            error_message=str(e),
            duration_seconds=duration,
            trace_id=trace_id,
            shopify_webhook_id=shopify_webhook_id,
            shopify_shop_domain=shopify_shop_domain
        )
        
        return {
            "success": False,
            "action": "error",
            "product_id": product_id,
            "error": str(e)
        }


async def handle_product_upsert(
    product_data: dict, 
    client_id: str, 
    trace_id: str, 
    is_create: bool = False,
    shopify_webhook_id: str = None,
    shopify_shop_domain: str = None,
    skip_llm_extraction: bool = False,
) -> Dict[str, Any]:
    """
    Handle product create/update - upsert to vector DB.
    
    Args:
        product_data: Shopify product webhook payload
        client_id: Client UUID
        trace_id: Request trace ID for logging
        is_create: True if this is a create event, False for update
        shopify_webhook_id: Shopify webhook ID
        shopify_shop_domain: Shop domain
        skip_llm_extraction: If True, skip LLM attribute extraction (used for
            inventory-only updates where product content hasn't changed)
        
    Returns:
        Result dictionary with success status
    """
    start_time = time.time()
    product_id = str(product_data.get('id', ''))
    product_title = product_data.get('title', 'Unknown')
    sync_type = "products/create" if is_create else "products/update"
    _caller_skip_llm = skip_llm_extraction
    
    try:
        from fashion_bot.services.product_ingestion.upstash_search_service import UpstashSearchService
        from fashion_bot.services.product_ingestion.shopify_product_service import ShopifyProductService
        from fashion_bot.services.product_ingestion.models import ShopifyConfig, NormalizedProduct
        from fashion_bot.services.product_ingestion.orchestrator import ProductIngestionOrchestrator
        from fashion_bot.config_manager import aget_shopify_config, aget_config
        
        product_status = product_data.get('status', 'active')
        published_at = product_data.get('published_at')
        
        action = "create" if is_create else "update"
        logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] 📦 Processing product {action}: {product_id} - {product_title}")
        
        # Skip if product is not active (draft, archived)
        if product_status != 'active':
            duration = time.time() - start_time
            logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] ⏭️ Skipping non-active product (status: {product_status})")
            
            # Log skipped product (not a failure, just filtered out)
            await ProductSyncLogger.alog_webhook_event(
                client_id=client_id,
                sync_type=sync_type,
                status=ProductSyncLogger.STATUS_SUCCESS,
                product_id=product_id,
                product_title=product_title,
                duration_seconds=duration,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain
            )
            
            return {
                "success": True,
                "action": "skipped",
                "product_id": product_id,
                "reason": f"Product status is {product_status}, not active"
            }
        
        # Product is active but unpublished (removed from storefront) —
        # delete from search index so customers don't see dead links.
        if not published_at:
            logger.info(
                f"[PRODUCT_WEBHOOK] [{trace_id}] 🗑️ Product {product_id} is active but "
                f"unpublished (published_at is null) — removing from search index"
            )
            delete_result = await handle_product_delete(
                product_id, client_id, trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain,
            )
            delete_result["reason"] = "Product is active but unpublished (published_at is null)"
            return delete_result
        
        # Get Shopify config for the client to use the normalizer
        shopify_config_dict = await aget_shopify_config(client_id=client_id)
        
        # Get website URL from config (try website_urls config)
        website_url = None
        website_config = await aget_config("website_urls", client_id=client_id)
        if website_config:
            if isinstance(website_config, dict):
                website_url = (
                    website_config.get("website_url") or
                    website_config.get("website") or
                    website_config.get("home") or
                    website_config.get("base_url")
                )
            elif isinstance(website_config, str):
                website_url = website_config
        
        shopify_config = ShopifyConfig(
            shop_domain=shopify_config_dict.get('shop_domain', shopify_config_dict.get('shop_url', '')),
            access_token=shopify_config_dict.get('access_token', ''),
            api_version=shopify_config_dict.get('api_version', '2024-04')
        )
        
        # Create service instance to use normalizer
        shopify_service = ShopifyProductService(shopify_config, website_base_url=website_url)
        
        # Convert webhook payload to GraphQL-like format for normalizer
        # Webhook format is different from GraphQL response, need to adapt
        graphql_format = _convert_webhook_to_graphql_format(product_data)
        
        # Enrich with collections + resolved metafields from Admin GraphQL
        # so the normalizer produces identical output to the full-sync path.
        # enriched_ok=False means the metafields are unreliable (transient
        # failure) — we preserve the prior Upstash values rather than wipe them.
        #
        # Skip the enrich call entirely for inventory-only updates
        # (skip_llm_extraction=True): collections, metafields, and SEO data
        # are irrelevant when only stock quantities changed, and the LLM
        # fields are preserved from Redis cache. This saves one Shopify
        # GraphQL call per inventory webhook.
        if not skip_llm_extraction:
            graphql_format, enriched_ok = await _enrich_webhook_with_graphql(
                graphql_format, client_id, trace_id,
            )
        else:
            enriched_ok = True

        # Normalize the product
        normalized_product = await shopify_service._normalize_product(graphql_format)

        # Extract current stock status from the normalized product
        new_in_stock = normalized_product.in_stock
        new_available_sizes = list(normalized_product.available_sizes) if normalized_product.available_sizes else []
        new_available_colors = list(normalized_product.available_colors) if normalized_product.available_colors else []

        # ── Redis cache check (hash + stock status) ──────────────────
        _cached_llm_fields = None
        _redis_cached = None
        _skip_upstash_upsert = False
        # Hash of just the extraction-prompt inputs; gates the LLM (the full
        # content hash keeps gating the Upstash upsert). Persisted alongside the
        # full hash so later webhooks can compare against it.
        _new_semantic_hash = normalized_product.compute_semantic_content_hash()

        try:
            from fashion_bot.services.product_ingestion.product_llm_cache import (
                aget_cached_product_data,
                aset_cached_product_data,
                llm_inputs_unchanged,
                stock_status_changed,
                LLM_EXTRACTED_CONTENT_FIELDS as _LLM_FIELDS,
            )
            _redis_cached = await aget_cached_product_data(client_id, product_id)
        except Exception as e:
            logger.warning(
                f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Redis cache GET failed: {e}"
            )

        if _redis_cached and not is_create:
            new_hash = normalized_product.compute_content_hash_excluding_inventory()
            old_hash = _redis_cached.get("content_hash_no_inventory")
            content_unchanged = old_hash and old_hash == new_hash
            # Two independent questions, previously conflated into one hash:
            #   content_unchanged     -> does the Upstash search doc need rewriting?
            #                            (price, discount, status, tags, images)
            #   _llm_inputs_unchanged -> is the cached LLM extraction still valid?
            #                            (title, product_type, description only)
            # A tag sweep, a price change or a throttled metafield enrich moves
            # the first and not the second — those used to re-extract entire
            # catalogues without altering a single thing the model reads.
            _llm_inputs_unchanged = llm_inputs_unchanged(
                _redis_cached, _new_semantic_hash, new_hash
            )
            _stock_changed = stock_status_changed(
                _redis_cached, new_in_stock, new_available_sizes, new_available_colors,
            )

            if _caller_skip_llm:
                # Caller already decided to skip LLM (e.g. inventory_levels/update).
                # Always use Redis-cached LLM fields regardless of hash match —
                # webhook REST payload may produce a different hash due to missing
                # metafields / collections, but the LLM fields are still valid.
                _cached_llm_fields = _redis_cached.get("llm_fields") or {}

                if not _stock_changed:
                    _skip_upstash_upsert = True
                    logger.info(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] ⏭️ Stock status unchanged "
                        f"(caller skip_llm=True, hash_match={content_unchanged}) — "
                        f"skipping Upstash upsert, using Redis LLM fields"
                    )

                    # Product-level stock can sit still while an individual
                    # variant flips (two colours sharing a size, a restock that
                    # re-adds a size Redis already listed). Repair the index
                    # before returning, or this branch strands it.
                    _receipt_early = await _arepair_variants_if_stale(
                        client_id=client_id,
                        product_id=product_id,
                        trace_id=trace_id,
                        normalized_product=normalized_product,
                        redis_cached=_redis_cached,
                    )

                    try:
                        _var_snap_early, _inv_maps_early = _build_variant_snapshot_from_product_data(product_data)
                        await aset_cached_product_data(
                            client_id, product_id,
                            old_hash or new_hash,
                            _cached_llm_fields, new_in_stock,
                            new_available_sizes, new_available_colors,
                            variant_snapshot=_var_snap_early or _redis_cached.get("variant_snapshot"),
                            # Preserve, never recompute: this payload skipped the
                            # GraphQL enrich, so its fabric/care_instructions are
                            # missing and a fresh semantic hash would be wrong.
                            semantic_content_hash=_redis_cached.get("semantic_content_hash"),
                            cron_owned_fields=_redis_cached.get("cron_owned_fields"),
                            upstash_variant_signature=_receipt_early,
                        )
                        if _inv_maps_early:
                            from fashion_bot.services.product_ingestion.product_llm_cache import aset_inventory_item_index
                            await aset_inventory_item_index(client_id, _inv_maps_early)
                    except Exception as e:
                        logger.warning(
                            f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Redis cache update failed: {e}"
                        )

                    duration = time.time() - start_time
                    await ProductSyncLogger.alog_webhook_event(
                        client_id=client_id,
                        sync_type=sync_type,
                        status=ProductSyncLogger.STATUS_SUCCESS,
                        product_id=product_id,
                        product_title=product_title,
                        duration_seconds=duration,
                        trace_id=trace_id,
                        shopify_webhook_id=shopify_webhook_id,
                        shopify_shop_domain=shopify_shop_domain,
                    )
                    return {
                        "success": True,
                        "action": "redis_only",
                        "product_id": product_id,
                        "title": product_title,
                    }
                else:
                    logger.info(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] 🔄 Stock status changed "
                        f"(in_stock={new_in_stock}, sizes={new_available_sizes}) — "
                        f"skipping LLM, using Redis LLM fields, upserting to Upstash Search"
                    )
            elif _llm_inputs_unchanged:
                skip_llm_extraction = True
                _cached_llm_fields = _redis_cached.get("llm_fields") or {}

                if content_unchanged and not _stock_changed:
                    _skip_upstash_upsert = True
                    logger.info(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] ⏭️ Content & stock unchanged "
                        f"(in_stock={new_in_stock}, sizes={new_available_sizes}) — "
                        f"skipping Upstash upsert, updating Redis only"
                    )

                    # This is the branch that fired repeatedly for the GANT
                    # green striped shirt: content identical, product-level
                    # stock identical, size M restocked and never reindexed.
                    _receipt_cu = await _arepair_variants_if_stale(
                        client_id=client_id,
                        product_id=product_id,
                        trace_id=trace_id,
                        normalized_product=normalized_product,
                        redis_cached=_redis_cached,
                    )

                    try:
                        _var_snap_cu, _inv_maps_cu = _build_variant_snapshot_from_product_data(product_data)
                        await aset_cached_product_data(
                            client_id, product_id, new_hash,
                            _cached_llm_fields, new_in_stock,
                            new_available_sizes, new_available_colors,
                            variant_snapshot=_var_snap_cu or _redis_cached.get("variant_snapshot"),
                            # Full hash matched, so every prompt field is identical
                            # too — safe to stamp, and this heals pre-migration
                            # entries onto the semantic hash.
                            semantic_content_hash=_new_semantic_hash,
                            cron_owned_fields=_redis_cached.get("cron_owned_fields"),
                            upstash_variant_signature=_receipt_cu,
                        )
                        if _inv_maps_cu:
                            from fashion_bot.services.product_ingestion.product_llm_cache import aset_inventory_item_index
                            await aset_inventory_item_index(client_id, _inv_maps_cu)
                    except Exception as e:
                        logger.warning(
                            f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Redis cache update failed: {e}"
                        )

                    duration = time.time() - start_time
                    await ProductSyncLogger.alog_webhook_event(
                        client_id=client_id,
                        sync_type=sync_type,
                        status=ProductSyncLogger.STATUS_SUCCESS,
                        product_id=product_id,
                        product_title=product_title,
                        duration_seconds=duration,
                        trace_id=trace_id,
                        shopify_webhook_id=shopify_webhook_id,
                        shopify_shop_domain=shopify_shop_domain,
                    )
                    return {
                        "success": True,
                        "action": "redis_only",
                        "product_id": product_id,
                        "title": product_title,
                    }
                else:
                    # Either the search doc moved (price/discount/status/image)
                    # or stock changed — reindex, but the prompt inputs are
                    # identical so the cached extraction still stands.
                    logger.info(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] 🔄 Non-LLM content changed "
                        f"(content_unchanged={bool(content_unchanged)}, "
                        f"stock_changed={_stock_changed}, in_stock={new_in_stock}, "
                        f"sizes={new_available_sizes}) — "
                        f"skipping LLM but upserting to Upstash Search"
                    )
            else:
                _fp = _hash_field_fingerprint(normalized_product)
                logger.info(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] 🔄 LLM inputs changed "
                    f"(hash old={(old_hash or '')[:8]}… new={new_hash[:8]}…, "
                    f"semantic new={_new_semantic_hash[:8]}…) — "
                    f"running LLM extraction | {_fp}"
                )
        elif not _redis_cached and not is_create:
            if _caller_skip_llm:
                logger.info(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] ℹ️ No Redis cache for product "
                    f"(caller skip_llm=True) — will preserve LLM fields from Upstash"
                )
            else:
                # No Redis cache — fall back to Upstash Search to check whether
                # only inventory changed. Avoids unnecessary LLM calls when a
                # products/update webhook fires alongside inventory_levels/update
                # and the Redis cache hasn't been populated yet.
                try:
                    _fb_search = UpstashSearchService()
                    _existing_doc = _fb_search.fetch_document_by_id(client_id, product_id)
                    if _existing_doc:
                        _existing_meta = _existing_doc.get("metadata") or {}
                        _existing_hash = _existing_meta.get("content_hash_no_inventory", "")
                        _new_hash_fb = normalized_product.compute_content_hash_excluding_inventory()
                        if _existing_hash and _existing_hash == _new_hash_fb:
                            skip_llm_extraction = True
                            _existing_content = _existing_doc.get("content") or {}
                            _cached_llm_fields = {
                                f: _existing_content.get(f)
                                for f in _LLM_EXTRACTED_CONTENT_FIELDS
                                if _existing_content.get(f)
                            }
                            logger.info(
                                f"[PRODUCT_WEBHOOK] [{trace_id}] ⏭️ Upstash hash match "
                                f"(no Redis cache) — skipping LLM, preserving "
                                f"{len(_cached_llm_fields)} existing fields"
                            )
                        else:
                            _fp = _hash_field_fingerprint(normalized_product)
                            logger.info(
                                f"[PRODUCT_WEBHOOK] [{trace_id}] ℹ️ No Redis cache, "
                                f"Upstash hash {'mismatch' if _existing_hash else 'missing'} "
                                f"— running LLM extraction | {_fp}"
                            )
                    else:
                        logger.info(
                            f"[PRODUCT_WEBHOOK] [{trace_id}] ℹ️ No Redis cache, "
                            f"no Upstash doc — running LLM extraction (cold start)"
                        )
                except Exception as _fb_err:
                    logger.warning(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Upstash hash fallback "
                        f"failed ({_fb_err}) — running LLM extraction"
                    )

        # Image-OCR pipeline on the webhook hot path.
        #
        # Delegates to the shared single-product OCR helper which encapsulates
        # the flag gate, Tier 1 / Tier 2 / LLM call, per-call timeout, and
        # the Upstash-preserve fallback that ensures the webhook never wipes
        # prior OCR data with empty values.
        #
        # The webhook uses the helper's default timeout
        # (``WEBHOOK_OCR_TIMEOUT_SECONDS`` in
        # ``services/product_ingestion/product_image_ocr_extractor.py``).
        # That default exceeds Shopify's 5 s webhook-response budget, so
        # cold-cache webhooks may trigger Shopify retries. Existing dedup
        # (``atry_claim_inventory_upsert`` + ``product_llm_cache``) absorbs
        # those idempotently.
        try:
            from fashion_bot.services.product_ingestion.product_image_ocr_extractor import (
                aapply_ocr_to_product,
            )
            ocr_diag = await aapply_ocr_to_product(
                client_id=client_id,
                normalized_product=normalized_product,
                trace_id=trace_id,
            )
            logger.info(
                f"[PRODUCT_WEBHOOK] [{trace_id}] 🖼️ OCR helper: "
                f"enabled={ocr_diag['enabled']} ran_llm={ocr_diag['ran_llm']} "
                f"tier1_hit={ocr_diag['tier1_hit']} "
                f"preserved={ocr_diag['preserved_from_upstash']} "
                f"images={ocr_diag['images_count']} "
                f"summary_chars={ocr_diag['summary_chars']}"
            )
        except Exception as e:
            logger.warning(
                f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Image OCR pipeline skipped: {e}"
            )

        if skip_llm_extraction:
            logger.info(
                f"[PRODUCT_WEBHOOK] [{trace_id}] ⏭️ Skipping LLM extraction (inventory-only update)"
            )
        else:
            # Extract structured attributes via LLM (Gemini 2.5 Flash Lite).
            # OCR cache read above populated normalized_product.image_ocr_texts,
            # which we now thread into the extractor prompt as image_text_excerpt
            # (concatenated per-image text, capped at ~1500 chars).
            try:
                from fashion_bot.services.product_ingestion.product_attribute_extractor import (
                    get_product_attribute_extractor,
                )
                from fashion_bot.services.product_ingestion.orchestrator import (
                    _build_image_text_excerpt,
                )
                extractor = get_product_attribute_extractor()

                # Build dict for LLM extraction — use original webhook payload which has
                # richer data (title, tags, product_type, body_html, options, etc.)
                extractor_dict = {
                    "title": product_data.get("title", ""),
                    "tags": product_data.get("tags", ""),
                    "collections": [],  # Not available in webhook payload
                    "category": product_data.get("product_type", ""),
                    "description": product_data.get("body_html", product_data.get("description", "")),
                    "template_suffix": product_data.get("template_suffix", ""),
                    "options": product_data.get("options", []),
                    "image_text_excerpt": _build_image_text_excerpt(
                        normalized_product.image_ocr_texts
                    ),
                }

                attrs = await extractor.extract_attributes(extractor_dict, client_id=client_id)

                if attrs.extraction_success:
                    ProductIngestionOrchestrator._apply_extracted_attributes(
                        [normalized_product], [attrs]
                    )
                    logger.info(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] 🧠 LLM extracted: "
                        f"product_line='{attrs.product_line}', color='{attrs.color}', "
                        f"material='{attrs.material}', category='{attrs.category}', "
                        f"occasion={attrs.occasion}"
                    )
                else:
                    logger.warning(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ LLM extraction failed: {attrs.error}"
                    )
                    report_error(
                        "Product webhook LLM extraction failed",
                        level='warning',
                        client_id=client_id,
                        product_id=product_id,
                        product_title=product_title,
                        trace_id=trace_id,
                        extraction_error=attrs.error,
                    )
            except Exception as e:
                logger.warning(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ LLM attribute extraction skipped: {e}"
                )
                report_error(
                    "Product webhook LLM extraction exception",
                    level='warning',
                    exc_info=(type(e), e, e.__traceback__),
                    client_id=client_id,
                    product_id=product_id,
                    product_title=product_title,
                    trace_id=trace_id,
                )

        # Upsert to Upstash Search
        search_doc = normalized_product.to_search_document(client_id)
        search_service = UpstashSearchService()

        if skip_llm_extraction:
            _preserve_llm_fields(
                search_doc, trace_id,
                cached_llm_fields=_cached_llm_fields,
                search_service=search_service,
                client_id=client_id,
                product_id=product_id,
            )

        # Unconditional: a webhook never carries the cron's bestseller flag or
        # engagement analytics, and the upsert replaces the whole document, so
        # these must be restored whether or not extraction ran.
        _preserve_cron_owned_fields(
            search_doc, trace_id,
            cached_entry=_redis_cached,
            search_service=search_service,
            client_id=client_id,
            product_id=product_id,
        )

        # When the Admin GraphQL enrich failed, the freshly-normalized doc has
        # empty collections/metafields. Carry those over from the existing
        # Upstash doc so a transient enrich failure can't wipe the size chart,
        # care instructions, SEO, etc. (No-op on cold ingestion / no prior doc.)
        if not enriched_ok:
            _preserve_enrich_fields(
                search_doc, trace_id,
                search_service=search_service,
                client_id=client_id,
                product_id=product_id,
            )

        # Dedup gate: for inventory-only changes, only the first webhook
        # (inventory_levels/update OR products/update) writes to Upstash.
        # Tracked past this block so a later stock patch in the same invocation
        # does not re-take a claim it already holds and lock itself out.
        _holds_upsert_claim = False
        if skip_llm_extraction and not is_create:
            try:
                from fashion_bot.services.product_ingestion.product_llm_cache import (
                    UPSTASH_VARIANT_SIGNATURE_KEY,
                    atry_claim_inventory_upsert,
                    aset_cached_product_data as _aset_cache,
                )
                claimed = await atry_claim_inventory_upsert(client_id, product_id)
                _holds_upsert_claim = bool(claimed)
                if not claimed:
                    logger.info(
                        f"[PRODUCT_WEBHOOK] [{trace_id}] ⏭️ Dedup: another webhook "
                        f"already upserted product {product_id} — skipping Upstash write"
                    )
                    # Still update Redis cache with latest stock status.
                    # Preserve the authoritative hash when caller skipped LLM.
                    try:
                        _dedup_hash = (
                            (_redis_cached.get("content_hash_no_inventory") if _caller_skip_llm and _redis_cached else None)
                            or normalized_product.compute_content_hash_excluding_inventory()
                        )
                        _var_snap_dd, _inv_maps_dd = _build_variant_snapshot_from_product_data(product_data)
                        await _aset_cache(
                            client_id, product_id,
                            _dedup_hash,
                            _cached_llm_fields or {},
                            new_in_stock, new_available_sizes, new_available_colors,
                            variant_snapshot=_var_snap_dd or (_redis_cached.get("variant_snapshot") if _redis_cached else None),
                            # We wrote nothing to Upstash — the webhook that won
                            # the claim did. Carry its receipt through untouched
                            # rather than vouching for a write we did not make.
                            upstash_variant_signature=(
                                _redis_cached.get(UPSTASH_VARIANT_SIGNATURE_KEY)
                                if _redis_cached else None
                            ),
                        )
                        if _inv_maps_dd:
                            from fashion_bot.services.product_ingestion.product_llm_cache import aset_inventory_item_index
                            await aset_inventory_item_index(client_id, _inv_maps_dd)
                    except Exception:
                        pass

                    duration = time.time() - start_time
                    await ProductSyncLogger.alog_webhook_event(
                        client_id=client_id,
                        sync_type=sync_type,
                        status=ProductSyncLogger.STATUS_SUCCESS,
                        product_id=product_id,
                        product_title=product_title,
                        duration_seconds=duration,
                        trace_id=trace_id,
                        shopify_webhook_id=shopify_webhook_id,
                        shopify_shop_domain=shopify_shop_domain,
                    )
                    return {
                        "success": True,
                        "action": "dedup_skipped",
                        "product_id": product_id,
                        "title": product_title,
                    }
            except Exception as e:
                logger.warning(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Dedup claim failed, "
                    f"proceeding with upsert: {e}"
                )

        content_size = len(
            json.dumps(search_doc.get("content") or {}, default=str).encode("utf-8")
        )
        if content_size > 4096:
            logger.warning(
                f"[PRODUCT_WEBHOOK] [{trace_id}] ⏭️  Skipping full Upstash upsert for "
                f"product {product_id}: content size {content_size} > 4096. "
                f"Content summarizer LLM is non-deterministic, so re-summarizing on "
                f"each webhook would burn cost. Deferring full content to weekly delta."
            )

            # Always refresh Redis inventory so the bot serves accurate stock between
            # weekly deltas. content_hash_no_inventory is deterministic (raw product
            # fields, no LLM), so caching it does not trigger re-sync churn.
            stock_patched = False
            try:
                from fashion_bot.services.product_ingestion.product_llm_cache import (
                    UPSTASH_VARIANT_SIGNATURE_KEY,
                    aset_cached_product_data,
                    aset_inventory_item_index,
                    collect_cron_owned_fields_from_doc,
                    collect_llm_fields_from_doc,
                )
                # Read back off the document AFTER the preserve steps, so
                # restored values are re-cached rather than decaying away.
                llm_fields_to_cache = collect_llm_fields_from_doc(search_doc)
                cron_fields_to_cache = collect_cron_owned_fields_from_doc(search_doc)
                if _caller_skip_llm and _redis_cached:
                    oversize_cache_hash = (
                        _redis_cached.get("content_hash_no_inventory")
                        or normalized_product.compute_content_hash_excluding_inventory()
                    )
                    # Un-enriched payload — keep the authoritative semantic hash.
                    oversize_semantic_hash = _redis_cached.get("semantic_content_hash")
                else:
                    oversize_cache_hash = (
                        normalized_product.compute_content_hash_excluding_inventory()
                    )
                    oversize_semantic_hash = _new_semantic_hash
                # Patch stock fields in Upstash only when a variant crossed the
                # in-stock/out-of-stock line since the index was last written
                # (or on the cold path with no Redis snapshot). Uses
                # abulk_update_fields, which fetches the existing compressed doc
                # and patches in place, so content size stays ≤ 4096 and the
                # summarizer is a no-op. Idempotent: same stock values produce
                # the same patched doc.
                #
                # Runs before the Redis write so the receipt reflects a write we
                # actually landed. `variants` rides along: patching the stock
                # rollup while leaving per-variant flags at their last full
                # upsert is precisely what let the widget strike out a size that
                # had been back in stock for days.
                _oversize_receipt = await _arepair_variants_if_stale(
                    client_id=client_id,
                    product_id=product_id,
                    trace_id=trace_id,
                    normalized_product=normalized_product,
                    redis_cached=_redis_cached,
                    already_claimed=_holds_upsert_claim,
                )
                stock_patched = bool(
                    _oversize_receipt
                    and _oversize_receipt != (_redis_cached or {}).get(UPSTASH_VARIANT_SIGNATURE_KEY)
                )

                _var_snap_os, _inv_maps_os = _build_variant_snapshot_from_product_data(product_data)
                await aset_cached_product_data(
                    client_id,
                    product_id,
                    oversize_cache_hash,
                    llm_fields_to_cache,
                    new_in_stock,
                    new_available_sizes,
                    new_available_colors,
                    variant_snapshot=_var_snap_os or None,
                    semantic_content_hash=oversize_semantic_hash,
                    cron_owned_fields=cron_fields_to_cache,
                    upstash_variant_signature=_oversize_receipt,
                )
                if _inv_maps_os:
                    await aset_inventory_item_index(client_id, _inv_maps_os)
            except Exception as e:
                stock_patched = False
                logger.warning(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] "
                    f"⚠️ Redis cache update on oversize-skip failed: {e}"
                )

            duration = time.time() - start_time
            await ProductSyncLogger.alog_webhook_event(
                client_id=client_id,
                sync_type=sync_type,
                status=ProductSyncLogger.STATUS_SUCCESS,
                product_id=product_id,
                product_title=product_title,
                duration_seconds=duration,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain,
            )
            return {
                "success": True,
                "action": "skipped_oversize_deferred_to_delta",
                "product_id": product_id,
                "title": product_title,
                "content_size": content_size,
                "stock_patched": stock_patched,
            }

        result = await search_service.aupsert_documents([search_doc], client_id=client_id)

        success_count = result.get('success_count', 0)
        duration = time.time() - start_time

        if success_count > 0:
            logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] ✅ Product {product_id} upserted to Search index")

            # Update Redis cache so future webhooks can skip Upstash reads/writes.
            # When the caller explicitly skipped LLM (e.g. inventory_levels/update),
            # the webhook data may lack metafields, producing a different hash.
            # Preserve the authoritative hash from the prior ingestion/products/update.
            try:
                from fashion_bot.services.product_ingestion.product_llm_cache import (
                    aset_cached_product_data,
                    aset_inventory_item_index,
                    collect_cron_owned_fields_from_doc,
                    collect_llm_fields_from_doc,
                    variant_availability_signature,
                )
                # Read back off the document AFTER the preserve steps, so
                # restored values are re-cached rather than decaying away.
                llm_fields_to_cache = collect_llm_fields_from_doc(search_doc)
                cron_fields_to_cache = collect_cron_owned_fields_from_doc(search_doc)
                if _caller_skip_llm and _redis_cached:
                    cache_hash = _redis_cached.get("content_hash_no_inventory") or \
                        normalized_product.compute_content_hash_excluding_inventory()
                    # Un-enriched payload — keep the authoritative semantic hash.
                    semantic_hash = _redis_cached.get("semantic_content_hash")
                else:
                    cache_hash = normalized_product.compute_content_hash_excluding_inventory()
                    semantic_hash = _new_semantic_hash
                _var_snap, _inv_maps = _build_variant_snapshot_from_product_data(product_data)
                await aset_cached_product_data(
                    client_id,
                    product_id,
                    cache_hash,
                    llm_fields_to_cache,
                    new_in_stock,
                    new_available_sizes,
                    new_available_colors,
                    variant_snapshot=_var_snap or None,
                    semantic_content_hash=semantic_hash,
                    cron_owned_fields=cron_fields_to_cache,
                    # The full upsert just replaced the document with exactly
                    # these variants, so this receipt is earned. Without it the
                    # next webhook would see "no receipt" and buy a redundant
                    # patch of a document that is already correct.
                    upstash_variant_signature=variant_availability_signature(
                        (search_doc.get("metadata") or {}).get("variants") or []
                    ),
                )
                if _inv_maps:
                    await aset_inventory_item_index(client_id, _inv_maps)
            except Exception as e:
                logger.warning(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Redis cache write failed: {e}"
                )

            # Log success - determine if it was an add or update
            products_added = 1 if is_create else 0
            products_updated = 0 if is_create else 1
            
            await ProductSyncLogger.alog_webhook_event(
                client_id=client_id,
                sync_type=sync_type,
                status=ProductSyncLogger.STATUS_SUCCESS,
                product_id=product_id,
                product_title=product_title,
                products_added=products_added,
                products_updated=products_updated,
                duration_seconds=duration,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain
            )
            
            return {
                "success": True,
                "action": action,
                "product_id": product_id,
                "title": product_title
            }
        else:
            logger.warning(f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Failed to upsert product {product_id}")
            
            # Report to Rollbar
            report_error(
                "Product webhook upsert returned zero success count",
                level='warning',
                client_id=client_id,
                product_id=product_id,
                product_title=product_title,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain,
                action=action,
            )
            
            # Log failure
            await ProductSyncLogger.alog_webhook_event(
                client_id=client_id,
                sync_type=sync_type,
                status=ProductSyncLogger.STATUS_FAILURE,
                product_id=product_id,
                product_title=product_title,
                products_failed=1,
                error_message="Upsert returned 0 success count",
                duration_seconds=duration,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain
            )
            
            return {
                "success": False,
                "action": action,
                "product_id": product_id,
                "error": "Upsert returned 0 success count"
            }
            
    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"[PRODUCT_WEBHOOK] [{trace_id}] ❌ Failed to upsert product: {e}", exc_info=True)
        
        # Report to Rollbar
        report_error(
            "Product webhook upsert failed",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            client_id=client_id,
            product_id=product_id,
            product_title=product_title,
            trace_id=trace_id,
            shopify_webhook_id=shopify_webhook_id,
            shopify_shop_domain=shopify_shop_domain,
            action=action,
        )
        
        # Log failure
        await ProductSyncLogger.alog_webhook_event(
            client_id=client_id,
            sync_type=sync_type,
            status=ProductSyncLogger.STATUS_FAILURE,
            product_id=product_id,
            product_title=product_title,
            products_failed=1,
            error_message=str(e),
            duration_seconds=duration,
            trace_id=trace_id,
            shopify_webhook_id=shopify_webhook_id,
            shopify_shop_domain=shopify_shop_domain
        )
        
        return {
            "success": False,
            "action": "error",
            "product_id": product_data.get('id', 'unknown'),
            "error": str(e)
        }


def _normalize_webhook_tags(raw_tags: Any) -> List[str]:
    """Normalize Shopify webhook ``tags`` to a clean list of non-empty strings.

    Shopify delivers ``tags`` as a comma-separated string (e.g. ``"a, b, c"``)
    in REST/webhook payloads. For products with no tags, the value is the empty
    string ``""``. A naive ``"".split(", ")`` returns ``[""]`` — a list with one
    empty string — which produces a different ``compute_content_hash_excluding_inventory``
    than the GraphQL/sync path (which yields ``[]``). The mismatch causes every
    products/update webhook for tag-less products to incorrectly fail the
    "content unchanged" check, triggering an LLM extraction + Upstash upsert on
    each event.

    Mirrors the cleanup in ``ShopifyProductService._normalize_product`` so both
    ingestion paths produce byte-identical hash inputs.
    """
    if raw_tags is None:
        return []
    if isinstance(raw_tags, str):
        return [t.strip() for t in raw_tags.split(",") if t.strip()]
    if isinstance(raw_tags, list):
        return [t.strip() for t in raw_tags if isinstance(t, str) and t.strip()]
    return []


def _convert_webhook_to_graphql_format(webhook_data: dict) -> dict:
    """
    Convert Shopify webhook product format to GraphQL-like format.
    
    Webhook and GraphQL responses have different structures.
    The normalizer expects GraphQL format with nested edges/nodes.
    
    Args:
        webhook_data: Product data from Shopify webhook
        
    Returns:
        Product data in GraphQL-like format
    """
    # Extract product options to map option1/2/3 to their names
    product_options = webhook_data.get('options', [])
    option_names = []
    for opt in product_options:
        if isinstance(opt, dict):
            option_names.append(opt.get('name', ''))
        elif isinstance(opt, str):
            option_names.append(opt)
    
    # Extract variants
    variants = webhook_data.get('variants', [])
    variant_edges = []
    
    for v in variants:
        # Build selectedOptions from option1, option2, option3
        selected_options = []
        for i, opt_value in enumerate([v.get('option1'), v.get('option2'), v.get('option3')]):
            if opt_value:
                opt_name = option_names[i] if i < len(option_names) else f"Option {i+1}"
                selected_options.append({"name": opt_name, "value": opt_value})
        
        variant_edges.append({
            "node": {
                "id": f"gid://shopify/ProductVariant/{v.get('id', '')}",
                "title": v.get('title', ''),
                "sku": v.get('sku', ''),
                "price": str(v.get('price', '0')),
                "compareAtPrice": str(v.get('compare_at_price', '')) if v.get('compare_at_price') else None,
                "inventoryQuantity": v.get('inventory_quantity', 0),
                "inventoryPolicy": v.get('inventory_policy', 'deny'),
                "selectedOptions": selected_options
            }
        })
    
    # Extract images
    images = webhook_data.get('images', [])
    image_edges = [
        {
            "node": {
                "url": img.get('src', ''),
                "altText": img.get('alt', '')
            }
        }
        for img in images
    ]
    
    # Extract options - handle both dict and string formats
    formatted_options = []
    for opt in product_options:
        if isinstance(opt, dict):
            formatted_options.append({
                "name": opt.get('name', ''),
                "values": opt.get('values', [])
            })
        elif isinstance(opt, str):
            formatted_options.append({
                "name": opt,
                "values": []
            })
    
    # Build GraphQL-like format
    #
    # Convert metafields from webhook format to GraphQL edge/node format.
    # When webhook is registered with metafield_namespaces, Shopify includes
    # metafields as a flat list: [{"key": "fabric", "value": "cotton",
    #   "namespace": "custom", "type": "single_line_text_field"}, ...]
    # The normalizer expects GraphQL format: {"edges": [{"node": {...}}, ...]}
    webhook_metafields = webhook_data.get('metafields', [])
    metafield_edges = []
    if isinstance(webhook_metafields, list):
        for mf in webhook_metafields:
            if isinstance(mf, dict):
                metafield_edges.append({
                    "node": {
                        "namespace": mf.get("namespace", ""),
                        "key": mf.get("key", ""),
                        "value": mf.get("value", ""),
                        "type": mf.get("type", "")
                    }
                })

    return {
        "id": f"gid://shopify/Product/{webhook_data.get('id', '')}",
        "title": webhook_data.get('title', ''),
        "description": "",  # Leave empty so normalizer falls through to descriptionHtml → _strip_html()
        "descriptionHtml": webhook_data.get('body_html', ''),
        "handle": webhook_data.get('handle', ''),
        "vendor": webhook_data.get('vendor', ''),
        "productType": webhook_data.get('product_type', ''),
        "tags": _normalize_webhook_tags(webhook_data.get('tags')),
        "createdAt": webhook_data.get('created_at', ''),
        "updatedAt": webhook_data.get('updated_at', ''),
        "status": webhook_data.get('status', 'active'),
        "totalInventory": sum(v.get('inventory_quantity', 0) for v in variants),
        "seo": {
            "title": webhook_data.get('title', ''),
            "description": webhook_data.get('body_html', '')[:160] if webhook_data.get('body_html') else ''
        },
        "metafields": {"edges": metafield_edges},  # Now includes metafields from webhook payload
        "options": formatted_options,
        "variants": {"edges": variant_edges},
        "images": {"edges": image_edges}
    }


@router.post("/webhook/products")
async def product_webhook(request: Request, client_id: str = None):
    """
    Handle Shopify product webhooks (create, update, delete).
    
    Shopify sends different topics:
    - products/create: New product created
    - products/update: Product updated  
    - products/delete: Product deleted
    
    This endpoint keeps the vector DB in sync with Shopify.
    
    Client ID Resolution (in order of priority):
    1. Query parameter: ?client_id=xxx
    2. X-Client-Id header (for testing)
    3. Lookup from X-Shopify-Shop-Domain header
    4. Fallback to default client
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trace_id = generate_trace_id()
    set_trace_id(trace_id)

    try:
        shopify_topic = request.headers.get("X-Shopify-Topic", "unknown")
        shopify_shop_domain = request.headers.get("X-Shopify-Shop-Domain", "unknown")
        shopify_webhook_id = request.headers.get("X-Shopify-Webhook-Id", "unknown")

        if is_shop_domain_blocklisted(shopify_shop_domain):
            logger.info(
                f"[PRODUCT_WEBHOOK] [{trace_id}] 🚫 Blocked webhook for blocklisted Shopify domain: "
                f"{shopify_shop_domain} topic={shopify_topic}"
            )
            return {"status": "blocked", "message": "Shop domain is blocklisted"}

        webhook_data = await request.json()

        # Resolve client_id (priority: query param > header > shop domain lookup > default)
        resolved_client_id = (
            client_id  # Query parameter
            or request.headers.get("X-Client-Id")  # Custom header for testing
            or await aget_client_id_from_shop_domain(shopify_shop_domain)  # Lookup from shop domain
        )
        set_request_client_id(resolved_client_id)

        if not resolved_client_id:
            # Always return 200 so Shopify doesn't retry indefinitely. But
            # the missing-mapping case is split:
            #   * domain on SHOPIFY_UNMAPPED_DOMAIN_ALLOWLIST → expected,
            #     debug log only.
            #   * anything else → real tenant-mapping bug, ERROR + Rollbar.
            normalized_domain = (shopify_shop_domain or "").strip().lower()
            allowlist = _shopify_unmapped_domain_allowlist()
            if normalized_domain in allowlist:
                logger.debug(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] Ignoring webhook from "
                    f"allowlisted unmapped shop '{shopify_shop_domain}'"
                )
            else:
                logger.error(
                    f"[PRODUCT_WEBHOOK] [{trace_id}] No client_id mapping for "
                    f"shop '{shopify_shop_domain}' (topic={shopify_topic}); "
                    f"add to SHOPIFY_UNMAPPED_DOMAIN_ALLOWLIST if intentional"
                )
                report_error(
                    "Product webhook could not resolve client_id",
                    level="error",
                    trace_id=trace_id,
                    shopify_shop_domain=shopify_shop_domain,
                    shopify_topic=shopify_topic,
                    shopify_webhook_id=shopify_webhook_id,
                )
            return {
                "status": "ignored",
                "reason": "shop domain not mapped to any client_id",
                "shop": shopify_shop_domain,
            }
        
        if is_client_blocklisted(resolved_client_id):
            logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] 🚫 Blocked webhook for blocklisted client_id: {resolved_client_id}")
            return {"status": "blocked", "message": "Client is blocklisted"}
        
        # Extract product info for logging
        product_id = str(webhook_data.get('id', 'unknown'))
        product_title = webhook_data.get('title', 'Unknown')
        
        # Log incoming webhook
        logger.info(f"{'='*80}")
        logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] Received: {shopify_topic}")
        logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] Shop: {shopify_shop_domain}")
        logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] Client: {resolved_client_id}")
        logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] Product ID: {product_id}")
        logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] Product Title: {product_title}")
        logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] Webhook ID: {shopify_webhook_id}")

        # Log the raw webhook payload as a single JSON line for diff/replay.
        # Mirrors the [WEBHOOK-IN] pattern used by the order webhook so the
        # same Loki query (`|= "WEBHOOK-IN" |= "<product_id>"`) works for both.
        try:
            payload_json = json.dumps(webhook_data, default=str, ensure_ascii=False)
            logger.info(
                f"[WEBHOOK-IN] [PRODUCT_WEBHOOK] [{trace_id}] "
                f"topic={shopify_topic} product_id={product_id} "
                f"payload_bytes={len(payload_json.encode('utf-8'))} "
                f"Payload: {payload_json}"
            )
        except Exception as _payload_log_err:
            logger.warning(
                f"[PRODUCT_WEBHOOK] [{trace_id}] ⚠️ Failed to serialize webhook "
                f"payload for logging: {_payload_log_err}"
            )
        logger.info(f"{'='*80}")
        
        print(f"\n{'='*80}")
        print(f"[{timestamp}] 📦 PRODUCT WEBHOOK - Topic: {shopify_topic}")
        print(f"  Shop: {shopify_shop_domain}")
        print(f"  Client: {resolved_client_id}")
        print(f"  Product: {product_id} - {product_title}")
        print(f"  Trace ID: {trace_id}")
        print(f"{'='*80}\n")
        
        # Route to appropriate handler based on topic. Each heavy handler is
        # wrapped in submit_or_inline so it is offloaded to a worker when its
        # lane is enabled, and run inline (unchanged) otherwise.
        if shopify_topic == "products/delete":
            result = await submit_or_inline(
                JOB_PRODUCT_DELETE,
                {
                    "product_id": product_id,
                    "client_id": resolved_client_id,
                    "trace_id": trace_id,
                    "shopify_webhook_id": shopify_webhook_id,
                    "shopify_shop_domain": shopify_shop_domain,
                },
                lambda: handle_product_delete(
                    product_id,
                    resolved_client_id,
                    trace_id,
                    shopify_webhook_id=shopify_webhook_id,
                    shopify_shop_domain=shopify_shop_domain,
                ),
            )

        elif shopify_topic in ["products/create", "products/update"]:
            is_create = shopify_topic == "products/create"
            result = await submit_or_inline(
                JOB_PRODUCT_UPSERT,
                {
                    "webhook_data": webhook_data,
                    "client_id": resolved_client_id,
                    "trace_id": trace_id,
                    "is_create": is_create,
                    "shopify_webhook_id": shopify_webhook_id,
                    "shopify_shop_domain": shopify_shop_domain,
                },
                lambda: handle_product_upsert(
                    webhook_data,
                    resolved_client_id,
                    trace_id,
                    is_create=is_create,
                    shopify_webhook_id=shopify_webhook_id,
                    shopify_shop_domain=shopify_shop_domain,
                ),
            )

        else:
            logger.warning(f"[PRODUCT_WEBHOOK] [{trace_id}] Unknown topic: {shopify_topic}")
            result = {
                "success": False,
                "error": f"Unknown topic: {shopify_topic}"
            }
        
        if result.get('success'):
            print(f"[{timestamp}] ✅ PRODUCT WEBHOOK SUCCESS - {result.get('action', 'processed')}: {product_id}")
            logger.info(f"[PRODUCT_WEBHOOK] [{trace_id}] ✅ Success: {result}")
        else:
            print(f"[{timestamp}] ❌ PRODUCT WEBHOOK FAILED - {result.get('error', 'Unknown error')}")
            logger.error(f"[PRODUCT_WEBHOOK] [{trace_id}] ❌ Failed: {result}")
        
        return {
            "status": "ok" if result.get('success') else "error",
            "topic": shopify_topic,
            "product_id": product_id,
            "client_id": resolved_client_id,
            **result
        }
        
    except Exception as e:
        logger.error(f"[PRODUCT_WEBHOOK] [{trace_id}] ❌ Exception: {e}", exc_info=True)
        print(f"[{timestamp}] ❌ PRODUCT WEBHOOK EXCEPTION: {str(e)}")
        report_error(
            "Product webhook unhandled exception",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            trace_id=trace_id,
        )
        return {
            "status": "error",
            "error": str(e)
        }


# ---------------------------------------------------------------------------
# Inventory Level Webhook — real-time stock sync
# ---------------------------------------------------------------------------

@router.post("/webhook/inventory")
async def inventory_webhook(request: Request, client_id: str = None):
    """
    Handle Shopify inventory_levels/update webhooks.

    Payload from Shopify:
        {"inventory_item_id": int, "location_id": int, "available": int, "updated_at": str}

    Flow:
        1. Resolve inventory_item_id -> variant -> product via Shopify REST API
        2. Fetch the full product via GraphQL
        3. Re-normalize and upsert to Vector + Search (same as products/update)
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trace_id = generate_trace_id()
    set_trace_id(trace_id)

    try:
        shopify_shop_domain = request.headers.get("X-Shopify-Shop-Domain", "unknown")
        shopify_webhook_id = request.headers.get("X-Shopify-Webhook-Id", "unknown")

        if is_shop_domain_blocklisted(shopify_shop_domain):
            logger.info(
                f"[INVENTORY_WEBHOOK] [{trace_id}] 🚫 Blocked webhook for blocklisted Shopify domain: "
                f"{shopify_shop_domain}"
            )
            return {"status": "blocked", "message": "Shop domain is blocklisted"}

        webhook_data = await request.json()

        inventory_item_id = webhook_data.get("inventory_item_id")
        available_qty = webhook_data.get("available")

        resolved_client_id = (
            client_id
            or request.headers.get("X-Client-Id")
            or await aget_client_id_from_shop_domain(shopify_shop_domain)
        )
        set_request_client_id(resolved_client_id)

        if not resolved_client_id:
            logger.error(
                f"[INVENTORY_WEBHOOK] [{trace_id}] Could not resolve client_id "
                f"shop_domain={shopify_shop_domain} webhook_id={shopify_webhook_id} "
                f"inventory_item_id={inventory_item_id}"
            )
            return {"status": "error", "error": "Could not resolve client_id"}

        if is_client_blocklisted(resolved_client_id):
            logger.info(f"[INVENTORY_WEBHOOK] [{trace_id}] 🚫 Blocked webhook for blocklisted client_id: {resolved_client_id}")
            return {"status": "blocked", "message": "Client is blocklisted"}

        logger.info(
            f"[INVENTORY_WEBHOOK] [{trace_id}] inventory_item={inventory_item_id} "
            f"available={available_qty} shop={shopify_shop_domain} client={resolved_client_id}"
        )

        result = await submit_or_inline(
            JOB_INVENTORY_UPDATE,
            {
                "inventory_item_id": inventory_item_id,
                "client_id": resolved_client_id,
                "trace_id": trace_id,
                "shopify_webhook_id": shopify_webhook_id,
                "shopify_shop_domain": shopify_shop_domain,
                "webhook_available": available_qty,
            },
            lambda: _handle_inventory_level_update(
                inventory_item_id=inventory_item_id,
                client_id=resolved_client_id,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain,
                webhook_available=available_qty,
            ),
        )

        if result.get("success"):
            logger.info(f"[INVENTORY_WEBHOOK] [{trace_id}] ✅ {result}")
        else:
            logger.warning(f"[INVENTORY_WEBHOOK] [{trace_id}] ⚠️ {result}")

        return {"status": "ok" if result.get("success") else "error", "client_id": resolved_client_id, **result}

    except Exception as e:
        logger.error(f"[INVENTORY_WEBHOOK] [{trace_id}] ❌ Exception: {e}", exc_info=True)
        report_error(
            "Inventory webhook unhandled exception",
            level="error",
            exc_info=(type(e), e, e.__traceback__),
            trace_id=trace_id,
        )
        return {"status": "error", "error": str(e)}


def _build_variant_snapshot_from_product_data(
    product_data: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """Build a compact variant snapshot from a raw Shopify REST product payload.

    Returns ``(snapshot, inv_mappings)`` where:
    - *snapshot* is a list of dicts suitable for caching in Redis (one per variant)
    - *inv_mappings* is a list of ``(inventory_item_id, product_id)`` tuples
      for the reverse-index cache
    """
    product_id = str(product_data.get("id", ""))
    variants_raw = product_data.get("variants") or []
    snapshot: List[Dict[str, Any]] = []
    inv_mappings: List[Tuple[int, str]] = []

    for v in variants_raw:
        inv_item_id = v.get("inventory_item_id")
        inv_qty = int(v.get("inventory_quantity", 0) or 0)
        inv_policy = v.get("inventory_policy", "deny")
        selected_options = []
        option_names = []
        for opt in (product_data.get("options") or []):
            if isinstance(opt, dict):
                option_names.append(opt.get("name", ""))
            elif isinstance(opt, str):
                option_names.append(opt)
        for i, opt_value in enumerate(
            [v.get("option1"), v.get("option2"), v.get("option3")]
        ):
            if opt_value:
                opt_name = option_names[i] if i < len(option_names) else f"Option {i+1}"
                selected_options.append({"name": opt_name, "value": opt_value})

        entry: Dict[str, Any] = {
            "variant_id": str(v.get("id", "")),
            "inventoryQuantity": inv_qty,
            "inventoryPolicy": inv_policy,
            "selectedOptions": selected_options,
        }
        if inv_item_id:
            entry["inventory_item_id"] = int(inv_item_id)
            inv_mappings.append((int(inv_item_id), product_id))
        snapshot.append(entry)

    return snapshot, inv_mappings


def _build_variant_snapshot_from_graphql(
    variant_nodes: List[Dict[str, Any]],
    product_id: str,
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]]]:
    """Build variant snapshot + reverse-index mappings from GraphQL variant nodes.

    The GraphQL ``inventoryItem.id`` lives on the variant node only if the
    query requested it.  Since the current ``_INVENTORY_RESOLVE_QUERY`` does
    *not* fetch ``inventoryItem { id }`` on *sibling* variants, this helper
    extracts the mapping only for variants that carry it.
    """
    snapshot: List[Dict[str, Any]] = []
    inv_mappings: List[Tuple[int, str]] = []

    for v in variant_nodes:
        inv_qty = int(v.get("inventoryQuantity", 0) or 0)
        inv_policy = v.get("inventoryPolicy", "")
        sel_opts = v.get("selectedOptions", [])
        vid = v.get("id", "")
        if "/" in vid:
            vid = vid.split("/")[-1]

        entry: Dict[str, Any] = {
            "variant_id": vid,
            "inventoryQuantity": inv_qty,
            "inventoryPolicy": inv_policy,
            "selectedOptions": sel_opts,
        }

        inv_item_node = v.get("inventoryItem") or {}
        inv_gid = inv_item_node.get("id", "")
        if "/" in inv_gid:
            inv_id = int(inv_gid.split("/")[-1])
            entry["inventory_item_id"] = inv_id
            inv_mappings.append((inv_id, product_id))

        snapshot.append(entry)

    return snapshot, inv_mappings


def _compute_stock_from_variants(
    variant_nodes: List[Dict[str, Any]],
) -> Tuple[bool, int, List[str], List[str]]:
    """Derive aggregate stock status from GraphQL variant nodes.

    Returns ``(in_stock, total_inventory, available_sizes, available_colors)``.
    Mirrors the logic in ``ShopifyProductService._normalize_product`` so the
    two paths stay byte-identical.
    """
    from fashion_bot.services.product_ingestion.shopify_product_service import (
        variant_is_in_stock,
    )

    in_stock = False
    total_inventory = 0
    avail_sizes: set = set()
    avail_colors: set = set()

    for v in variant_nodes:
        inv_qty = v.get("inventoryQuantity", 0)
        inv_policy = v.get("inventoryPolicy", "")
        total_inventory += int(inv_qty or 0)
        if variant_is_in_stock(inv_qty, inv_policy):
            in_stock = True
            for opt in v.get("selectedOptions", []):
                name = opt.get("name", "").lower()
                value = opt.get("value", "")
                if "size" in name:
                    avail_sizes.add(value)
                elif "color" in name or "colour" in name:
                    avail_colors.add(value)

    return in_stock, total_inventory, sorted(avail_sizes), sorted(avail_colors)


async def _apatch_upstash_variant_stock(
    *,
    client_id: str,
    product_id: str,
    trace_id: str,
    variant_dicts: List[Dict[str, Any]],
    in_stock: bool,
    total_inventory: int,
    available_sizes: List[str],
    available_colors: List[str],
) -> bool:
    """Patch the stock projection of one Upstash Search document in place.

    The single place that writes stock to the index. Callers must have already
    established, via ``upstash_variants_stale``, that a variant actually crossed
    the in-stock/out-of-stock boundary — this function does not re-check, it
    just writes. ``variant_availability_pct`` ships in the same patch because it
    is derived from the very flags in ``variant_dicts``; splitting them lets the
    search filter and the add-to-cart panel disagree about the same product.

    Returns *True* when the patch was applied, so the caller knows whether it
    may stamp the receipt.
    """
    from fashion_bot.services.product_ingestion.product_llm_cache import (
        variant_availability_pct,
    )
    from fashion_bot.services.product_ingestion.upstash_search_service import (
        UpstashSearchService,
    )

    if not variant_dicts:
        # No variants resolved means the upstream fetch degraded, not that the
        # product has none. Writing through would blank metadata.variants and
        # stamp variant_availability_pct at 100 — worse than leaving the
        # document alone until a webhook arrives with real data.
        logger.warning(
            f"[STOCK_PATCH] [{trace_id}] ⚠️ No variants resolved for product "
            f"{product_id} — skipping stock patch rather than blanking the doc"
        )
        return False

    try:
        result = await UpstashSearchService().abulk_update_fields(
            client_id=client_id,
            updates=[{
                "product_id": product_id,
                "content_fields": {
                    "in_stock": in_stock,
                    "total_inventory": total_inventory,
                    "available_sizes": available_sizes,
                    "available_colors": available_colors,
                    "variant_availability_pct": variant_availability_pct(variant_dicts),
                },
                "metadata_fields": {
                    "variants": variant_dicts[:20],
                },
            }],
        )
        if result.get("updated_count", 0) > 0:
            logger.info(
                f"[STOCK_PATCH] [{trace_id}] ✅ Patched Upstash Search for product "
                f"{product_id} (in_stock={in_stock}, sizes={available_sizes})"
            )
            return True
        logger.info(
            f"[STOCK_PATCH] [{trace_id}] ℹ️ Stock patch skipped — product "
            f"{product_id} not yet in Upstash index; delta sync will ingest it"
        )
        return False
    except Exception as e:
        logger.warning(
            f"[STOCK_PATCH] [{trace_id}] ⚠️ Upstash Search stock patch failed "
            f"for product {product_id}: {e}"
        )
        return False


async def _arepair_variants_if_stale(
    *,
    client_id: str,
    product_id: str,
    trace_id: str,
    normalized_product,
    redis_cached: Optional[Dict[str, Any]],
    already_claimed: bool = False,
) -> str:
    """Bring a skipped-upsert product's stock projection back in line, if needed.

    Called from the ``products/update`` branches that deliberately skip the
    Upstash upsert because the searchable *content* did not move. Content being
    unchanged says nothing about whether a variant sold out or came back, and
    those branches used to return having written Redis only — which is how a
    restock could land in Redis, satisfy every later "did it change?" check, and
    never reach the index.

    Costs one Upstash write only when a variant actually crossed the
    in-stock/out-of-stock line since the last write. Returns the receipt to
    persist: the new signature when we patched, the previous one otherwise.

    ``already_claimed`` must be set by callers downstream of the dedup gate in
    ``handle_product_upsert``: the claim is a SET NX on a per-product key, so a
    second attempt from the same invocation loses to its own key and would
    silently skip the patch.
    """
    from fashion_bot.services.product_ingestion.product_llm_cache import (
        UPSTASH_VARIANT_SIGNATURE_KEY,
        atry_claim_inventory_upsert,
        upstash_variants_stale,
        variant_availability_signature,
    )

    variants = list(getattr(normalized_product, "variants", None) or [])
    new_signature = variant_availability_signature(variants)
    prior_signature = (redis_cached or {}).get(UPSTASH_VARIANT_SIGNATURE_KEY)

    if not variants or not upstash_variants_stale(redis_cached, new_signature):
        return prior_signature

    # Shopify sends products/update and inventory_levels/update for the same
    # change; the claim keeps exactly one of them writing.
    if not already_claimed and not await atry_claim_inventory_upsert(client_id, product_id):
        logger.info(
            f"[PRODUCT_WEBHOOK] [{trace_id}] ⏭️ Dedup: another webhook already "
            f"claimed the variant-stock patch for {product_id}"
        )
        return prior_signature

    logger.info(
        f"[PRODUCT_WEBHOOK] [{trace_id}] 🔄 Variant availability drifted from the "
        f"index for product {product_id} — patching stock fields only"
    )
    patched = await _apatch_upstash_variant_stock(
        client_id=client_id,
        product_id=product_id,
        trace_id=trace_id,
        variant_dicts=variants,
        in_stock=normalized_product.in_stock,
        total_inventory=normalized_product.total_inventory,
        available_sizes=list(normalized_product.available_sizes or []),
        available_colors=list(normalized_product.available_colors or []),
    )
    return new_signature if patched else prior_signature


def _build_variant_dicts(variant_nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build compact variant dicts matching the metadata shape stored in Upstash.

    Accepts both GraphQL variant nodes (``id`` field with GID) and cached
    variant snapshots (``variant_id`` field, plain numeric string).
    """
    from fashion_bot.services.product_ingestion.shopify_product_service import (
        variant_is_in_stock,
    )
    result = []
    for v in variant_nodes:
        inv_qty = int(v.get("inventoryQuantity", 0) or 0)
        inv_policy = v.get("inventoryPolicy", "")
        sel_opts = v.get("selectedOptions", [])
        vid = v.get("variant_id") or v.get("id", "")
        if "/" in vid:
            vid = vid.split("/")[-1]
        result.append({
            "id": vid,
            "title": " / ".join(o.get("value", "") for o in sel_opts),
            "inventory_quantity": inv_qty,
            "available": variant_is_in_stock(inv_qty, inv_policy),
            "selected_options": sel_opts,
            "option1": sel_opts[0]["value"] if len(sel_opts) > 0 else None,
            "option2": sel_opts[1]["value"] if len(sel_opts) > 1 else None,
            "option3": sel_opts[2]["value"] if len(sel_opts) > 2 else None,
        })
    return result


_INVENTORY_RESOLVE_QUERY = """\
{
  inventoryItem(id: "gid://shopify/InventoryItem/%s") {
    variant {
      id
      inventoryQuantity
      inventoryPolicy
      selectedOptions { name value }
      product {
        id
        title
        status
        totalInventory
        variants(first: 100) {
          edges {
            node {
              id
              inventoryQuantity
              inventoryPolicy
              selectedOptions { name value }
              inventoryItem { id }
            }
          }
        }
      }
    }
  }
}
"""


async def _handle_inventory_level_update(
    inventory_item_id: int,
    client_id: str,
    trace_id: str,
    shopify_webhook_id: str = None,
    shopify_shop_domain: str = None,
    webhook_available: int = None,
) -> Dict[str, Any]:
    """Update stock status for a product after an inventory level change.

    **Warm path (zero Shopify API calls)**:
    When the Redis cache contains both the ``product_id`` reverse-index
    *and* a ``variant_snapshot``, stock is recomputed entirely in-process
    by patching the webhook's ``available`` quantity into the snapshot.

    **Cold path (one GraphQL call)**:
    Falls back to a single GraphQL call that resolves
    ``inventory_item → variant → product`` and fetches every variant's
    stock.  After resolving, it populates both caches so subsequent
    webhooks hit the warm path.

    **Cold-cold path (full product fetch)**:
    When the product has no Redis cache at all, falls back to the full
    ``handle_product_upsert`` path.
    """
    from fashion_bot.config_manager import aget_shopify_config
    from fashion_bot.utils.http_client import get_shared_async_http_client
    from fashion_bot.utils.shopify_rate_limiter import get_shopify_rate_limiter
    from fashion_bot.services.product_ingestion.product_llm_cache import (
        aget_cached_product_data,
        aget_product_id_by_inventory_item,
        aset_cached_product_data,
        aset_inventory_item_index,
        atry_claim_inventory_upsert,
    )

    start = time.time()

    try:
        # ── Warm path: resolve product_id from Redis reverse index ──
        product_id = await aget_product_id_by_inventory_item(
            client_id, inventory_item_id,
        )
        redis_cached = None
        variant_snapshot = None

        if product_id:
            redis_cached = await aget_cached_product_data(client_id, product_id)
            if redis_cached:
                variant_snapshot = redis_cached.get("variant_snapshot")

        if product_id and redis_cached and variant_snapshot:
            # ── Full warm path: zero Shopify calls ──
            updated_snapshot = []
            for v in variant_snapshot:
                v_copy = dict(v)
                if v_copy.get("inventory_item_id") == inventory_item_id:
                    if webhook_available is not None:
                        v_copy["inventoryQuantity"] = int(webhook_available)
                updated_snapshot.append(v_copy)

            new_in_stock, new_total_inventory, new_avail_sizes, new_avail_colors = (
                _compute_stock_from_variants(updated_snapshot)
            )

            product_title = "(cached)"
            logger.info(
                f"[INVENTORY_WEBHOOK] [{trace_id}] 🚀 Warm path: resolved "
                f"inventory_item {inventory_item_id} → product {product_id} "
                f"from Redis (0 Shopify calls)"
            )

            return await _inventory_stock_diff_and_patch(
                client_id=client_id,
                product_id=product_id,
                product_title=product_title,
                trace_id=trace_id,
                redis_cached=redis_cached,
                new_in_stock=new_in_stock,
                new_total_inventory=new_total_inventory,
                new_avail_sizes=new_avail_sizes,
                new_avail_colors=new_avail_colors,
                updated_snapshot=updated_snapshot,
                start=start,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain,
            )

        # ── Cold path: GraphQL resolve + fetch all variants ──
        logger.info(
            f"[INVENTORY_WEBHOOK] [{trace_id}] ℹ️ Cache miss for "
            f"inventory_item {inventory_item_id}"
            f"{' (product_id=' + product_id + ' but no snapshot)' if product_id else ''}"
            f" — falling back to GraphQL"
        )

        config = await aget_shopify_config(client_id=client_id)
        shop_url = config.get("shop_url") or config.get("shop_domain", "")
        token = config.get("access_token", "")
        api_ver = config.get("api_version", "2024-04")

        if not shop_url or not token:
            return {"success": False, "error": "Missing Shopify credentials for client"}

        headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }
        base = f"https://{shop_url}/admin/api/{api_ver}"
        http = await get_shared_async_http_client()
        limiter = get_shopify_rate_limiter()

        gql_query = _INVENTORY_RESOLVE_QUERY % inventory_item_id
        await limiter.acquire(shop_url)
        gql_resp = await http.post(
            f"{base}/graphql.json",
            headers=headers,
            json={"query": gql_query},
            timeout=10,
        )
        if gql_resp.status_code != 200:
            return {
                "success": False,
                "error": (
                    f"GraphQL lookup failed for inventory_item "
                    f"{inventory_item_id}: HTTP {gql_resp.status_code}"
                ),
            }

        inv_item = gql_resp.json().get("data", {}).get("inventoryItem") or {}
        variant_node = inv_item.get("variant") or {}
        product_node = variant_node.get("product") or {}
        product_gid = product_node.get("id", "")
        product_id = product_gid.split("/")[-1] if "/" in product_gid else None
        product_title = product_node.get("title", "?")
        product_status = (product_node.get("status") or "ACTIVE").lower()

        variant_gid = variant_node.get("id", "")
        variant_id = variant_gid.split("/")[-1] if "/" in variant_gid else None

        if not product_id:
            return {
                "success": False,
                "error": f"Could not resolve product for inventory_item {inventory_item_id}",
            }

        logger.info(
            f"[INVENTORY_WEBHOOK] [{trace_id}] Resolved inventory_item "
            f"{inventory_item_id} → variant {variant_id} → product {product_id} "
            f"({product_title}) via GraphQL"
        )

        if product_status != "active":
            duration = time.time() - start
            logger.info(
                f"[INVENTORY_WEBHOOK] [{trace_id}] ⏭️ Skipping non-active "
                f"product (status: {product_status})"
            )
            await ProductSyncLogger.alog_webhook_event(
                client_id=client_id,
                sync_type="inventory_levels/update",
                status=ProductSyncLogger.STATUS_SUCCESS,
                product_id=str(product_id),
                product_title=product_title,
                duration_seconds=duration,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain,
            )
            return {
                "success": True,
                "action": "skipped",
                "product_id": product_id,
                "reason": f"Product status is {product_status}",
            }

        variant_edges = product_node.get("variants", {}).get("edges", [])
        variant_nodes = [e["node"] for e in variant_edges if e.get("node")]

        if not variant_nodes:
            return {
                "success": False,
                "error": f"No variants returned for product {product_id}",
            }

        new_in_stock, new_total_inventory, new_avail_sizes, new_avail_colors = (
            _compute_stock_from_variants(variant_nodes)
        )

        # Populate caches from GraphQL response so next webhook is warm.
        gql_snapshot, gql_inv_maps = _build_variant_snapshot_from_graphql(
            variant_nodes, product_id,
        )
        if gql_inv_maps:
            try:
                await aset_inventory_item_index(client_id, gql_inv_maps)
            except Exception:
                pass

        if not redis_cached:
            redis_cached = await aget_cached_product_data(client_id, product_id)

        if not redis_cached:
            # Cold-cold: no product cache at all → full upsert path.
            logger.info(
                f"[INVENTORY_WEBHOOK] [{trace_id}] ℹ️ No Redis cache for "
                f"product {product_id} — falling back to full upsert path"
            )
            await limiter.acquire(shop_url)
            prod_resp = await http.get(
                f"{base}/products/{product_id}.json",
                headers={"X-Shopify-Access-Token": token},
                timeout=10,
            )
            if prod_resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"Failed to fetch product {product_id}: HTTP {prod_resp.status_code}",
                }
            product_data = prod_resp.json().get("product", {})
            result = await handle_product_upsert(
                product_data,
                client_id,
                trace_id,
                is_create=False,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain,
                skip_llm_extraction=True,
            )
            duration = time.time() - start
            await ProductSyncLogger.alog_webhook_event(
                client_id=client_id,
                sync_type="inventory_levels/update",
                status=(
                    ProductSyncLogger.STATUS_SUCCESS
                    if result.get("success")
                    else ProductSyncLogger.STATUS_FAILURE
                ),
                product_id=str(product_id),
                product_title=product_title,
                duration_seconds=duration,
                trace_id=trace_id,
                shopify_webhook_id=shopify_webhook_id,
                shopify_shop_domain=shopify_shop_domain,
            )
            return result

        return await _inventory_stock_diff_and_patch(
            client_id=client_id,
            product_id=product_id,
            product_title=product_title,
            trace_id=trace_id,
            redis_cached=redis_cached,
            new_in_stock=new_in_stock,
            new_total_inventory=new_total_inventory,
            new_avail_sizes=new_avail_sizes,
            new_avail_colors=new_avail_colors,
            updated_snapshot=gql_snapshot or None,
            start=start,
            shopify_webhook_id=shopify_webhook_id,
            shopify_shop_domain=shopify_shop_domain,
        )

    except Exception as e:
        logger.error(f"[INVENTORY_WEBHOOK] [{trace_id}] ❌ {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def _inventory_stock_diff_and_patch(
    *,
    client_id: str,
    product_id: str,
    product_title: str,
    trace_id: str,
    redis_cached: Dict[str, Any],
    new_in_stock: bool,
    new_total_inventory: int,
    new_avail_sizes: List[str],
    new_avail_colors: List[str],
    updated_snapshot: Optional[List[Dict[str, Any]]],
    start: float,
    shopify_webhook_id: str = None,
    shopify_shop_domain: str = None,
) -> Dict[str, Any]:
    """Refresh Redis, and patch Upstash when a variant's purchasability flipped.

    Shared by both the warm (zero-API) path and the cold (GraphQL) path.

    The gate is the *index receipt*, not the Redis stock fields. Redis tracks
    Shopify and is refreshed by several paths that skip Upstash on purpose;
    gating on it meant a restock landing after a sell-out was read as "no
    change" and the index kept serving the sold-out flag forever.
    """
    from fashion_bot.services.product_ingestion.product_llm_cache import (
        UPSTASH_VARIANT_SIGNATURE_KEY,
        aset_cached_product_data,
        atry_claim_inventory_upsert,
        upstash_variants_stale,
        variant_availability_signature,
    )

    variant_dicts = _build_variant_dicts(updated_snapshot) if updated_snapshot else []
    new_signature = variant_availability_signature(variant_dicts)
    prior_signature = (redis_cached or {}).get(UPSTASH_VARIANT_SIGNATURE_KEY)

    if not variant_dicts or not upstash_variants_stale(redis_cached, new_signature):
        duration = time.time() - start
        logger.info(
            f"[INVENTORY_WEBHOOK] [{trace_id}] ⏭️ No variant crossed in/out of "
            f"stock for product {product_id} — no Upstash write needed"
        )
        await ProductSyncLogger.alog_webhook_event(
            client_id=client_id,
            sync_type="inventory_levels/update",
            status=ProductSyncLogger.STATUS_SUCCESS,
            product_id=str(product_id),
            product_title=product_title,
            duration_seconds=duration,
            trace_id=trace_id,
            shopify_webhook_id=shopify_webhook_id,
            shopify_shop_domain=shopify_shop_domain,
        )
        return {
            "success": True,
            "action": "no_change",
            "product_id": product_id,
            "title": product_title,
        }

    logger.info(
        f"[INVENTORY_WEBHOOK] [{trace_id}] 🔄 Variant availability changed for "
        f"product {product_id} (in_stock={new_in_stock}, "
        f"sizes={new_avail_sizes}) — patching"
    )

    claimed = await atry_claim_inventory_upsert(client_id, product_id)

    patched = False
    if claimed:
        patched = await _apatch_upstash_variant_stock(
            client_id=client_id,
            product_id=product_id,
            trace_id=trace_id,
            variant_dicts=variant_dicts,
            in_stock=new_in_stock,
            total_inventory=new_total_inventory,
            available_sizes=new_avail_sizes,
            available_colors=new_avail_colors,
        )
    else:
        logger.info(
            f"[INVENTORY_WEBHOOK] [{trace_id}] ⏭️ Dedup: another webhook "
            f"already claimed Upstash write for {product_id}"
        )

    try:
        await aset_cached_product_data(
            client_id,
            product_id,
            redis_cached.get("content_hash_no_inventory", ""),
            redis_cached.get("llm_fields") or {},
            new_in_stock,
            new_avail_sizes,
            new_avail_colors,
            variant_snapshot=updated_snapshot or redis_cached.get("variant_snapshot"),
            # Inventory-only patch: carry the semantic hash through untouched.
            # Dropping it would silently demote the entry to full-hash gating.
            semantic_content_hash=redis_cached.get("semantic_content_hash"),
            cron_owned_fields=redis_cached.get("cron_owned_fields"),
            # Stamp the receipt only for a write we actually landed. On a dedup
            # loss the winner writes the same signature; if our Redis write
            # happens to land last we carry the older receipt forward and buy
            # one redundant idempotent patch on the next webhook — the safe way
            # round, since claiming a write we did not make is what stranded the
            # index in the first place.
            upstash_variant_signature=new_signature if patched else prior_signature,
        )
    except Exception as cache_err:
        logger.warning(
            f"[INVENTORY_WEBHOOK] [{trace_id}] ⚠️ Redis cache update "
            f"failed: {cache_err}"
        )

    duration = time.time() - start
    await ProductSyncLogger.alog_webhook_event(
        client_id=client_id,
        sync_type="inventory_levels/update",
        status=ProductSyncLogger.STATUS_SUCCESS,
        product_id=str(product_id),
        product_title=product_title,
        duration_seconds=duration,
        trace_id=trace_id,
        shopify_webhook_id=shopify_webhook_id,
        shopify_shop_domain=shopify_shop_domain,
    )

    return {
        "success": True,
        "action": "stock_patched",
        "product_id": product_id,
        "title": product_title,
    }
