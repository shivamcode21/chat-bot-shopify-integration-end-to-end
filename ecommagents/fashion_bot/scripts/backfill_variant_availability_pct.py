#!/usr/bin/env python3
"""
One-time backfill: populate the numeric ``variant_availability_pct`` field on
existing Upstash Search product documents.

Why: the search layer ranks/filters products by what share of their variants are
in stock (so products with most sizes available surface first, instead of a
heavily-discounted item with only 1 live size). New writes get the field
automatically via ``NormalizedProduct.to_search_document`` (full sync, delta
sync, and the inventory webhook patch), but documents ingested BEFORE the field
was added lack it — and a ``variant_availability_pct > 50`` filter excludes docs
missing the field.

This backfill derives the percentage from each doc's existing ``variants``
metadata (already stored, each carrying an ``available`` flag), so it is cheap:
no Shopify calls, no LLM, no re-extraction.

Run it once per environment after deploying the field. Idempotent — docs whose
stored value already matches the computed one are skipped.

Usage:
    # Preview (no writes):
    python scripts/backfill_variant_availability_pct.py --dry-run

    # Backfill all Shopify-backed clients:
    python scripts/backfill_variant_availability_pct.py

    # Backfill a single client:
    python scripts/backfill_variant_availability_pct.py --client-id YOUR_CLIENT_UUID
"""

import os
import sys
import asyncio
import argparse

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from fashion_bot.services.product_ingestion.upstash_search_service import (
    get_upstash_search_service,
)


def _pct_from_variants(variants) -> float:
    """Share of variants in stock (0-100), rounded to 1 dp.

    Prefers each variant's stored ``available`` flag (written at ingestion via
    variant_is_in_stock, which honours oversell policy). Falls back to
    ``inventory_quantity > 0`` for very old docs that predate the flag. Returns
    0.0 when there are no variants.
    """
    if not variants:
        return 100.0
    available = 0
    for v in variants:
        if not isinstance(v, dict):
            continue
        if "available" in v:
            if v.get("available"):
                available += 1
        else:
            try:
                if int(v.get("inventory_quantity") or 0) > 0:
                    available += 1
            except (TypeError, ValueError):
                pass
    return round(available / len(variants) * 100, 1)


async def backfill_client(client_id: str, dry_run: bool = False) -> dict:
    """Backfill variant_availability_pct for one client's Upstash documents.

    Fetches every doc (prefix scan), derives the percentage from the doc's stored
    ``variants`` metadata, and re-upserts only the docs that are missing it (or
    whose value drifted). Returns a small summary dict.
    """
    search = get_upstash_search_service()
    # fetch_documents is a sync prefix scan; keep the event loop free.
    docs = await asyncio.to_thread(search.fetch_documents, client_id)

    to_update = []
    skipped_present = 0
    for doc in docs or []:
        content = dict(doc.get("content") or {})
        metadata = doc.get("metadata") or {}
        pct = _pct_from_variants(metadata.get("variants"))
        if content.get("variant_availability_pct") == pct:
            skipped_present += 1
            continue
        content["variant_availability_pct"] = pct
        to_update.append({
            "id": doc.get("id"),
            "content": content,
            "metadata": metadata,
        })

    summary = {
        "client_id": client_id,
        "total": len(docs or []),
        "to_backfill": len(to_update),
        "already_present": skipped_present,
    }

    if to_update and not dry_run:
        result = await search.aupsert_documents(to_update, client_id=client_id)
        summary["upserted"] = result.get("success_count", 0)
        summary["failed"] = len(result.get("failed", []))

    print(f"[backfill] {summary}")
    return summary


async def _amain(client_id: str | None, dry_run: bool) -> None:
    if client_id:
        client_ids = [client_id]
    else:
        from fashion_bot.cron_jobs.product_vector_sync_job import (
            get_all_client_ids_with_shopify,
        )
        client_ids = await get_all_client_ids_with_shopify()

    print(f"[backfill] {len(client_ids)} client(s) | dry_run={dry_run}")
    totals = {"total": 0, "to_backfill": 0, "upserted": 0}
    for cid in client_ids:
        try:
            s = await backfill_client(cid, dry_run=dry_run)
            totals["total"] += s.get("total", 0)
            totals["to_backfill"] += s.get("to_backfill", 0)
            totals["upserted"] += s.get("upserted", 0)
        except Exception as e:
            print(f"[backfill] ❌ client={cid} failed: {e}")
    print(f"[backfill] DONE totals={totals}")


def main():
    parser = argparse.ArgumentParser(description="Backfill variant_availability_pct on Upstash Search docs")
    parser.add_argument("--client-id", default=None, help="Backfill a single client (default: all Shopify clients)")
    parser.add_argument("--dry-run", action="store_true", help="Preview counts without writing")
    args = parser.parse_args()
    asyncio.run(_amain(args.client_id, args.dry_run))


if __name__ == "__main__":
    main()
