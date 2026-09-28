"""
Bestseller Refresh Cron Job — keep the Upstash Search ``bestseller`` flag in
sync with real Shopify sales.

Runs EVERY 3 DAYS. For each Shopify-backed client it:

1. Fetches the top ``BESTSELLER_TOP_N`` products sold on Shopify in the last
   ``BESTSELLER_LOOKBACK_DAYS`` days (3 months). Both the top-N count and the
   lookback window can be overridden per client via the ``client_configs`` keys
   ``bestseller_top_n`` and ``bestseller_lookback_days``.
2. Reads the products currently flagged ``bestseller = true`` in Upstash Search.
3. Reconciles the two sets by product_id:
     - ADD    → products that are now bestsellers but not yet flagged.
     - REMOVE → products flagged bestseller that are no longer in the window
                (their ``bestseller`` field is set back to ``false``).
     - SKIP   → products in both sets are left untouched (no write).

Upstash Search has no field-level update, so each changed document is fetched,
its ``bestseller`` field patched, and the whole doc (content + metadata)
re-upserted. Writes are kept minimal:
  * REMOVE reuses the docs already returned by the flagged-document scan — no
    extra reads. That scan is a full cursor walk of the client's index, which is
    the only complete way to enumerate a flag in Upstash Search (see
    ``fetch_documents_by_content_flag``); it is the one unavoidably broad read
    in this job, and it runs once per client per run.
  * ADD fetches ONLY the new ids via ``afetch_documents_by_ids`` (id lookups,
    not a full-catalog prefix scan).
  * A single batched ``aupsert_documents`` call writes adds + removes together.

Scheduling/placement (see scheduler.py): registered by ``start_cron_scheduler``
on every pod that boots the app (in practice the ``ecommagents-webhook-handlers``
Render service), on a restart-immune 3-day grid anchored at 04:00 UTC. A Redis
lease ensures a single pod runs it even when that service has several replicas.
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from fashion_bot.monitoring.otel_metrics import (
    track_cron,
    cron_run_counter,
    cron_duration,
    cron_items_processed,
    request_client_id,
)
from fashion_bot.cron_jobs.cron_lock import (
    acquire_cron_lock,
    release_cron_lock,
    get_cron_lock_owner,
)
# Reuse the exact tenant-discovery query used by the weekly vector sync so the
# two jobs never drift on which clients are "Shopify-backed".
from fashion_bot.cron_jobs.product_vector_sync_job import get_all_client_ids_with_shopify
from fashion_bot.trace_context import generate_trace_id, set_trace_id

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, min_value: int = 1) -> int:
    try:
        return max(min_value, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# Redis distributed-lock settings — only one pod runs the monthly refresh.
LOCK_KEY = "bestseller_refresh_lock"
LOCK_TTL_SECONDS = 10800  # 3 hours — covers a sequential sweep across all clients

# Tunables (env-overridable; some also per-client — see below).
# BESTSELLER_LOOKBACK_DAYS and BESTSELLER_TOP_N are env-level defaults that a
# client can override via client_configs keys ``bestseller_lookback_days`` and
# ``bestseller_top_n`` respectively (read per-client in _refresh_client_bestsellers).
BESTSELLER_LOOKBACK_DAYS = _env_int("BESTSELLER_LOOKBACK_DAYS", 90)
BESTSELLER_TOP_N = _env_int("BESTSELLER_TOP_N", 100)
# 50 orders/page; 200 pages ≈ 10k orders — enough to rank a true 3-month window
# for high-volume stores without an unbounded Shopify GraphQL spend.
BESTSELLER_MAX_ORDER_PAGES = _env_int("BESTSELLER_MAX_ORDER_PAGES", 200)


def _numeric_product_id(value: Any) -> str:
    """Normalise a Shopify product reference to its numeric id.

    Shopify GraphQL returns GIDs (``gid://shopify/Product/123``) while Upstash
    stores the numeric id in ``metadata.product_id``. Reduce both to ``"123"``
    so the two fetches compare correctly.
    """
    s = str(value or "").strip()
    if not s:
        return ""
    return s.split("/")[-1] if "/" in s else s


async def _refresh_client_bestsellers(client_id: str) -> Dict[str, Any]:
    """Reconcile the Upstash ``bestseller`` flag with Shopify sales for one client."""
    from fashion_bot.services.product_ingestion.product_source_factory import (
        ProductSourceFactory,
    )
    from fashion_bot.services.product_ingestion.shopify_product_service import (
        ShopifyProductService,
    )
    from fashion_bot.services.product_ingestion.upstash_search_service import (
        get_upstash_search_service,
    )

    # 1. Resolve the Shopify product service (skip non-Shopify clients).
    try:
        product_service = await ProductSourceFactory().aget_service(client_id, source="shopify")
    except ValueError:
        logger.info(f"[BESTSELLER_REFRESH] client={client_id} has no Shopify config — skipping")
        return {"client_id": client_id, "skipped": True, "reason": "no_shopify"}
    if not isinstance(product_service, ShopifyProductService):
        return {"client_id": client_id, "skipped": True, "reason": "not_shopify"}

    # Per-client lookback-window override (config key: bestseller_lookback_days)
    #  → env default. Resolved before the fetch so it drives both the sales
    # window and the log messages below.
    lookback_days = BESTSELLER_LOOKBACK_DAYS
    try:
        from fashion_bot.config_manager import aget_config
        client_lookback = await aget_config("bestseller_lookback_days", client_id=client_id)
        if client_lookback is not None:
            lookback_days = max(1, int(client_lookback))
            logger.info(
                f"[BESTSELLER_REFRESH] client={client_id} using per-client "
                f"lookback_days={lookback_days}"
            )
    except (TypeError, ValueError):
        pass
    except Exception as e:
        logger.warning(
            f"[BESTSELLER_REFRESH] client={client_id} lookback config read failed: {e}; "
            f"using default lookback_days={lookback_days}"
        )

    # 2. Top sellers on Shopify over the lookback window. afetch_sales_volume
    #    backs off on rate limits and reports whether the sweep completed.
    try:
        fetch = await product_service.afetch_sales_volume(
            days=lookback_days,
            max_pages=BESTSELLER_MAX_ORDER_PAGES,
        )
    except Exception as e:
        # Non-throttle HTTP/API error — never strip bestsellers on bad data.
        logger.error(
            f"[BESTSELLER_REFRESH] client={client_id} Shopify sales fetch failed: {e} — skipping"
        )
        return {"client_id": client_id, "skipped": True, "reason": "shopify_error"}

    sales = fetch.sales
    if not sales:
        # No sales data (new store or empty window). Skip — making no changes is
        # safer than wiping every existing bestseller on an empty fetch.
        logger.warning(
            f"[BESTSELLER_REFRESH] client={client_id} no Shopify sales in last "
            f"{lookback_days}d — skipping (no changes)"
        )
        return {"client_id": client_id, "skipped": True, "reason": "no_sales"}

    # Per-client top-N override (config key: bestseller_top_n) → env default.
    top_n = BESTSELLER_TOP_N
    try:
        from fashion_bot.config_manager import aget_config
        client_top_n = await aget_config("bestseller_top_n", client_id=client_id)
        if client_top_n is not None:
            top_n = max(1, int(client_top_n))
            logger.info(f"[BESTSELLER_REFRESH] client={client_id} using per-client top_n={top_n}")
    except (TypeError, ValueError):
        pass
    except Exception as e:
        logger.warning(f"[BESTSELLER_REFRESH] client={client_id} config read failed: {e}; using default top_n={top_n}")

    new_top = sorted(sales.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    new_ids: Set[str] = {_numeric_product_id(gid) for gid, _ in new_top}
    new_ids.discard("")

    # 3. Products currently flagged bestseller=true in Upstash (full docs).
    #    This is a cursor scan over the client's id prefix, NOT a search: Upstash
    #    Search has no filtered scan, `index.search(filter=...)` is a
    #    relevance-ranked top-K with no offset (so it silently caps at one page),
    #    and `index.range()` paginates but takes no filter. See
    #    fetch_documents_by_content_flag.
    #    The scan raises if the index is unreadable, so a transport error can
    #    never masquerade as "nothing is flagged" and strip every bestseller.
    search_service = get_upstash_search_service()
    try:
        current_docs = await search_service.afetch_documents_by_content_flag(
            client_id, "bestseller"
        )
    except Exception as e:
        logger.error(
            f"[BESTSELLER_REFRESH] client={client_id} could not enumerate current "
            f"bestsellers: {e} — skipping (no changes)"
        )
        return {"client_id": client_id, "skipped": True, "reason": "index_read_error"}

    current_by_id: Dict[str, Dict[str, Any]] = {}
    for doc in current_docs:
        pid = _numeric_product_id((doc.get("metadata") or {}).get("product_id", ""))
        if pid:
            current_by_id[pid] = doc
    current_ids: Set[str] = set(current_by_id.keys())

    # 4. Reconcile.
    to_add_ids = new_ids - current_ids
    to_remove_ids = current_ids - new_ids
    unchanged = len(new_ids & current_ids)

    # Completeness guard: a partial/throttled sweep undercounts sales, so it must
    # never DEMOTE a product (a real bestseller could sit on an un-fetched page).
    # Adds stay safe — a product in the partial top-N is genuinely a strong
    # seller — so on an incomplete sweep we apply adds only and skip removals.
    removals_skipped = 0
    removal_guard = False
    if not fetch.complete and to_remove_ids:
        removals_skipped = len(to_remove_ids)
        logger.warning(
            f"[BESTSELLER_REFRESH] client={client_id} Shopify sweep incomplete "
            f"(reason={fetch.truncated_reason}, pages={fetch.pages_fetched}); "
            f"skipping {removals_skipped} removal(s), applying adds only"
        )
        to_remove_ids = set()

    # Blast-radius guard: two non-empty id sets that share NOTHING is not a
    # catalogue-wide turnover, it is a structural mismatch (wrong id space,
    # wrong store, empty-but-readable index). Acting on it would clear every
    # bestseller flag the client has, which cannot be reconstructed from the
    # index afterwards. Adds stay safe, so apply those and skip the removals.
    # Any genuine churn leaves at least one product in both sets.
    elif to_remove_ids and unchanged == 0:
        removals_skipped = len(to_remove_ids)
        removal_guard = True
        logger.error(
            f"[BESTSELLER_REFRESH] client={client_id} zero overlap between "
            f"{len(new_ids)} Shopify top-seller id(s) and {len(current_ids)} "
            f"flagged id(s) — refusing to clear all {removals_skipped} flag(s). "
            f"Check that the sales fetch returns Shopify product ids."
        )
        to_remove_ids = set()

    docs_to_upsert: List[Dict[str, Any]] = []

    # REMOVE — reuse already-fetched docs; set bestseller=false (matches how
    # ingestion represents non-bestsellers). No extra reads.
    for pid in to_remove_ids:
        doc = current_by_id[pid]
        content = dict(doc.get("content") or {})
        content["bestseller"] = False
        docs_to_upsert.append({
            "id": doc.get("id"),
            "content": content,
            "metadata": doc.get("metadata") or {},
        })

    # ADD — fetch ONLY the new ids' docs, set bestseller=true.
    added = 0
    missing = 0
    if to_add_ids:
        try:
            add_docs = await search_service.afetch_documents_by_ids(
                client_id, list(to_add_ids)
            )
        except Exception as e:
            # An unreadable index must not be recorded as "these products are
            # not indexed" — that misreports a lookup outage as a data gap.
            # Abort before writing anything, including the pending removals.
            logger.error(
                f"[BESTSELLER_REFRESH] client={client_id} bestseller id lookup "
                f"failed: {e} — skipping (no changes)"
            )
            return {"client_id": client_id, "skipped": True, "reason": "index_read_error"}
        found_ids: Set[str] = set()
        for doc in add_docs:
            doc_id = doc.get("id")
            pid = _numeric_product_id((doc.get("metadata") or {}).get("product_id", ""))
            if not doc_id or not pid:
                continue
            found_ids.add(pid)
            content = dict(doc.get("content") or {})
            if content.get("bestseller") is True:
                continue
            content["bestseller"] = True
            docs_to_upsert.append({
                "id": doc_id,
                "content": content,
                "metadata": doc.get("metadata") or {},
            })
            added += 1
        missing = len(to_add_ids - found_ids)
        if missing:
            logger.info(
                f"[BESTSELLER_REFRESH] client={client_id} {missing} new bestseller "
                f"id(s) not found in index (not ingested yet) — skipped"
            )

    # 5. Single batched write (aupsert_documents chunks internally at 50).
    upserted = 0
    if docs_to_upsert:
        result = await search_service.aupsert_documents(docs_to_upsert, client_id=client_id)
        upserted = result.get("success_count", 0)
        failed = result.get("failed", [])
        if failed:
            logger.warning(
                f"[BESTSELLER_REFRESH] client={client_id} {len(failed)} doc upsert(s) failed"
            )

        # The product webhook restores bestseller/analytics from Redis, because
        # Upstash upsert replaces the whole document and a webhook payload
        # carries neither. Re-sync that cache here or a demotion written above
        # would be undone by the next webhook restoring a stale `true`.
        try:
            from fashion_bot.services.product_ingestion.product_llm_cache import (
                aupdate_cached_cron_fields,
            )
            synced = await aupdate_cached_cron_fields(client_id, docs_to_upsert)
            logger.info(
                f"[BESTSELLER_REFRESH] client={client_id} re-synced cron-owned "
                f"fields in {synced} Redis cache entrie(s)"
            )
        except Exception as cache_err:
            # Best-effort: a stale cache costs accuracy, not availability.
            logger.warning(
                f"[BESTSELLER_REFRESH] client={client_id} cron-field cache "
                f"re-sync failed: {cache_err}"
            )

    logger.info(
        f"[BESTSELLER_REFRESH] client={client_id} "
        f"shopify_top={len(new_ids)} current_flagged={len(current_ids)} "
        f"added={added} removed={len(to_remove_ids)} unchanged={unchanged} "
        f"missing={missing} removals_skipped={removals_skipped} "
        f"removal_guard={removal_guard} complete={fetch.complete} upserted={upserted}"
    )
    return {
        "client_id": client_id,
        "success": True,
        "added": added,
        "removed": len(to_remove_ids),
        "unchanged": unchanged,
        "missing": missing,
        "removals_skipped": removals_skipped,
        "removal_guard": removal_guard,
        "complete": fetch.complete,
        "upserted": upserted,
    }


async def _persist_client_log(client_id: str, result: Dict[str, Any], duration_seconds: float) -> None:
    """Persist a per-client row to ``product_sync_logs`` (best-effort, never raises).

    Reuses ``ProductSyncLogger.alog_cron_event`` (source=``cron``,
    sync_type=``bestseller_refresh``). Bestseller-flag adds map to
    ``products_added`` and removals to ``products_deleted``; ``products_failed``
    carries new ids not yet in the index. Status reflects completeness:
    a truncated Shopify sweep (adds-only) is logged as ``partial_failure``.
    """
    from fashion_bot.services.product_ingestion.sync_logger import ProductSyncLogger
    from fashion_bot.trace_context import get_trace_id_from_context

    reason = result.get("reason")
    # Non-applicable clients (no Shopify service) aren't worth a DB row.
    if result.get("skipped") and reason in ("no_shopify", "not_shopify"):
        return

    trace_id = get_trace_id_from_context()
    if trace_id == "-":
        trace_id = None

    added = removed = unchanged = failed = 0
    if result.get("skipped"):
        if reason == "shopify_error":
            status = ProductSyncLogger.STATUS_FAILURE
            notes = "Shopify sales fetch failed"
        elif reason == "index_read_error":
            status = ProductSyncLogger.STATUS_FAILURE
            notes = "Upstash Search index could not be read"
        else:  # no_sales — ran fine, nothing to reconcile
            status = ProductSyncLogger.STATUS_SUCCESS
            notes = f"skipped: {reason}"
    elif result.get("success"):
        added = result.get("added", 0)
        removed = result.get("removed", 0)
        unchanged = result.get("unchanged", 0)
        failed = result.get("missing", 0)
        complete = result.get("complete", True)
        removal_guard = result.get("removal_guard", False)
        status = (
            ProductSyncLogger.STATUS_SUCCESS if complete and not removal_guard
            else ProductSyncLogger.STATUS_PARTIAL_FAILURE
        )
        note_bits: List[str] = []
        if not complete:
            note_bits.append(f"sweep_incomplete; removals_skipped={result.get('removals_skipped', 0)}")
        if removal_guard:
            note_bits.append(
                f"zero id overlap; refused to clear "
                f"{result.get('removals_skipped', 0)} flag(s)"
            )
        if failed:
            note_bits.append(f"{failed} new bestseller id(s) not in index")
        notes = "; ".join(note_bits) or None
    else:
        status = ProductSyncLogger.STATUS_FAILURE
        notes = str(result.get("error", "unknown error"))[:500]

    try:
        await ProductSyncLogger.alog_cron_event(
            client_id=client_id,
            sync_type="bestseller_refresh",
            status=status,
            products_added=added,
            products_deleted=removed,      # bestseller flag cleared
            products_unchanged=unchanged,
            products_failed=failed,
            error_message=notes,
            duration_seconds=duration_seconds,
            trace_id=trace_id,
        )
    except Exception as e:
        logger.warning(f"[BESTSELLER_REFRESH] client={client_id} sync-log persist failed: {e}")


def _record_client_cron_metrics(client_id: str, result: Dict[str, Any], duration_seconds: float) -> None:
    """Emit per-client ``cron_run_counter`` / ``cron_duration`` (labelled with
    ``client_id``).

    These reuse the same OTel instruments as ``track_cron`` but add a
    ``client_id`` label, so per-client run counts and durations are available in
    Prometheus. The job-level series track_cron emits carry no ``client_id``
    (empty in Prometheus), so aggregate queries filter ``client_id=""`` while
    per-client dashboards filter on the specific client. ``status`` is one of
    ``success`` | ``partial`` (truncated sweep or blocked removals, adds-only) |
    ``failure`` (Shopify fetch or index read failed) | ``skipped``
    (non-Shopify / no sales in window).
    """
    if result.get("skipped"):
        status = (
            "failure"
            if result.get("reason") in ("shopify_error", "index_read_error")
            else "skipped"
        )
    elif result.get("success"):
        status = (
            "success"
            if result.get("complete", True) and not result.get("removal_guard")
            else "partial"
        )
    else:
        status = "failure"
    labels = {"job_name": "bestseller_refresh", "client_id": client_id, "status": status}
    cron_run_counter.add(1, labels)
    cron_duration.record(duration_seconds, labels)


async def _async_bestseller_refresh() -> Dict[str, Any]:
    """Run the bestseller reconciliation for every Shopify-backed client."""
    start_time = datetime.now(timezone.utc)

    logger.info("=" * 60)
    logger.info(f"[BESTSELLER_REFRESH] 🚀 Starting monthly refresh at {start_time.isoformat()}")
    logger.info("=" * 60)

    client_ids = await get_all_client_ids_with_shopify()
    if not client_ids:
        logger.info("[BESTSELLER_REFRESH] No clients to refresh")
        return {"success": True, "clients_processed": 0, "message": "No Shopify clients"}

    results: List[Dict[str, Any]] = []
    total_added = total_removed = total_failed = total_skipped = 0

    for client_id in client_ids:
        logger.info(f"[BESTSELLER_REFRESH] 🔄 Processing client: {client_id}")
        client_t0 = time.monotonic()
        # Scope OTel baggage so downstream metrics carry the right client_id.
        with request_client_id(client_id):
            try:
                result = await _refresh_client_bestsellers(client_id)
            except Exception as e:
                logger.error(f"[BESTSELLER_REFRESH] ❌ client={client_id} failed: {e}", exc_info=True)
                result = {"client_id": client_id, "success": False, "error": str(e)}
            client_duration = time.monotonic() - client_t0
            # Per-client audit row in product_sync_logs (best-effort).
            await _persist_client_log(client_id, result, client_duration)
            # Per-client cron run/duration metrics (client_id-labelled).
            _record_client_cron_metrics(client_id, result, client_duration)
        results.append(result)

        if result.get("skipped"):
            total_skipped += 1
        elif result.get("success"):
            total_added += result.get("added", 0)
            total_removed += result.get("removed", 0)
            client_items = result.get("added", 0) + result.get("removed", 0)
            if client_items:
                cron_items_processed.add(
                    client_items,
                    {"job_name": "bestseller_refresh", "client_id": client_id},
                )
        else:
            total_failed += 1

    end_time = datetime.now(timezone.utc)
    duration = (end_time - start_time).total_seconds()

    summary = {
        "success": total_failed == 0,
        "clients_processed": len(client_ids),
        "clients_failed": total_failed,
        "clients_skipped": total_skipped,
        "total_added": total_added,
        "total_removed": total_removed,
        "duration_seconds": duration,
        "started_at": start_time.isoformat(),
        "completed_at": end_time.isoformat(),
        "results": results,
    }

    logger.info("=" * 60)
    logger.info(f"[BESTSELLER_REFRESH] ✅ Completed in {duration:.2f}s")
    logger.info(
        f"[BESTSELLER_REFRESH] 📊 Clients: {len(client_ids)} processed, "
        f"{total_failed} failed, {total_skipped} skipped | "
        f"+{total_added} added, -{total_removed} removed"
    )
    logger.info("=" * 60)
    return summary


@track_cron("bestseller_refresh", items_key=["total_added", "total_removed"])
async def bestseller_refresh_monthly() -> Dict[str, Any]:
    """Cron entry point — native async, invoked by AsyncIOScheduler on the app loop.

    Acquires a Redis lease (LOCK_KEY) so that when the scheduler fires on
    multiple pods of the webhook-handlers service, only the pod that wins SETNX
    executes; the others log a skip and return.
    """
    set_trace_id(generate_trace_id())

    lease = None
    try:
        lease = await acquire_cron_lock(lock_key=LOCK_KEY, ttl_seconds=LOCK_TTL_SECONDS)
        if not lease:
            current_owner = await get_cron_lock_owner(LOCK_KEY)
            logger.info(
                "[BESTSELLER_REFRESH] Already running on another pod or lock backend "
                "unavailable. owner=%s",
                current_owner or "unknown",
            )
            return {
                "success": True,
                "skipped": True,
                "clients_processed": 0,
                "message": "Skipped: refresh already running on another pod",
                "lock_owner": current_owner,
            }

        logger.info("[BESTSELLER_REFRESH] Lock acquired by %s", lease.owner_id)
        return await _async_bestseller_refresh()
    except Exception as e:
        logger.error(f"[BESTSELLER_REFRESH] ❌ Job failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
    finally:
        if lease:
            try:
                await release_cron_lock(lease)
            except Exception as exc:
                logger.warning("[BESTSELLER_REFRESH] Failed to release lock: %s", exc)


async def refresh_single_client_bestsellers(client_id: str) -> Dict[str, Any]:
    """Manual trigger for a single client (admin endpoint / backfill)."""
    logger.info(f"[BESTSELLER_REFRESH] 🔄 Manual refresh triggered for client: {client_id}")
    with request_client_id(client_id):
        return await _refresh_client_bestsellers(client_id)
