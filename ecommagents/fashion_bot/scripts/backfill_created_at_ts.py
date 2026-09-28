#!/usr/bin/env python3
"""
One-time backfill: populate the numeric ``created_at_ts`` field on existing
Upstash Search product documents.

Why: the new-arrivals search filters on ``created_at_ts`` (epoch seconds) because
Upstash range operators (``>=``) apply to numbers, not ISO strings. New writes
get the field automatically via ``ProductDocument.to_search_document`` (all
ingestion/webhook/sync flows), but documents ingested BEFORE the field was added
lack it — and a ``created_at_ts >= X`` filter excludes docs missing the field.
This backfill derives ``created_at_ts`` from each doc's existing ``created_at``
(already stored), so it is cheap: no Shopify calls, no LLM, no re-extraction.

Run it once per environment after deploying the field. Idempotent — docs that
already have ``created_at_ts`` are skipped.

Usage:
    # Preview (no writes):
    python scripts/backfill_created_at_ts.py --dry-run

    # Backfill all Shopify-backed clients:
    python scripts/backfill_created_at_ts.py

    # Backfill a single client:
    python scripts/backfill_created_at_ts.py --client-id YOUR_CLIENT_UUID
"""

import os
import sys
import asyncio
import argparse

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from fashion_bot.services.product_ingestion.models import iso_to_epoch
from fashion_bot.services.product_ingestion.upstash_search_service import (
    get_upstash_search_service,
)


async def backfill_client(client_id: str, dry_run: bool = False) -> dict:
    """Backfill created_at_ts for one client's Upstash documents.

    Fetches every doc (prefix scan), derives created_at_ts from the doc's stored
    created_at, and re-upserts only the docs that are missing it (or whose value
    drifted). Returns a small summary dict.
    """
    search = get_upstash_search_service()
    # fetch_documents is a sync prefix scan; keep the event loop free.
    docs = await asyncio.to_thread(search.fetch_documents, client_id)

    to_update = []
    skipped_present = 0
    skipped_no_date = 0
    for doc in docs or []:
        content = dict(doc.get("content") or {})
        ts = iso_to_epoch(content.get("created_at"))
        if ts is None:
            skipped_no_date += 1
            continue
        if content.get("created_at_ts") == ts:
            skipped_present += 1
            continue
        content["created_at_ts"] = ts
        to_update.append({
            "id": doc.get("id"),
            "content": content,
            "metadata": doc.get("metadata") or {},
        })

    summary = {
        "client_id": client_id,
        "total": len(docs or []),
        "to_backfill": len(to_update),
        "already_present": skipped_present,
        "no_created_at": skipped_no_date,
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
    parser = argparse.ArgumentParser(description="Backfill created_at_ts on Upstash Search docs")
    parser.add_argument("--client-id", default=None, help="Backfill a single client (default: all Shopify clients)")
    parser.add_argument("--dry-run", action="store_true", help="Preview counts without writing")
    args = parser.parse_args()
    asyncio.run(_amain(args.client_id, args.dry_run))


if __name__ == "__main__":
    main()
