"""One-time backfill: populate empty Upstash ``content.handle`` from ``metadata.handle``.

Root cause: ``handle`` (and ``created_at_ts``) were added to the Upstash Search
``content`` document on 2026-06-08 (commit a016029a). Before that, ``handle`` lived
only in ``metadata``. The delta-sync content hash (``compute_content_hash*``) does
NOT include ``handle`` and carries no schema version, so the schema change did not
invalidate any product's hash — every product not independently re-ingested since
then was skipped and kept its old, handle-less content. Upstash Search filters on
``content`` only, so those products cannot be found by handle.

This script fills ``content.handle = metadata.handle`` (and ``created_at_ts`` from
``metadata.created_at``) for any doc missing it. Safe & idempotent: it only touches
docs where ``content.handle`` is empty AND ``metadata.handle`` is set, writing the
doc's own known-correct handle.

Usage (dry-run is the default; pass --apply to write):
    python -m scripts.backfill_content_handle --client <client_id> [--client <id> ...]
    python -m scripts.backfill_content_handle --client <client_id> --apply

This talks to Upstash directly (only needs UPSTASH_SEARCH_REST_URL/TOKEN), so it can
run standalone without the full app runtime.
"""
import argparse
import os
from datetime import datetime

from upstash_search import Search


def _iso_to_epoch(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def backfill_client(client: Search, client_id: str, apply: bool) -> tuple[int, int]:
    idx = client.index(f"client_{client_id}")
    prefix = f"{client_id}_"
    cursor = ""
    scanned = 0
    to_fix = []
    while True:
        res = idx.range(cursor=cursor, limit=100, prefix=prefix)
        for d in getattr(res, "documents", None) or []:
            scanned += 1
            ct = dict(getattr(d, "content", None) or {})
            md = getattr(d, "metadata", None) or {}
            mh = md.get("handle")
            if not ct.get("handle") and mh:
                to_fix.append((d, ct, md, mh))
        cursor = getattr(res, "next_cursor", "") or ""
        if not cursor:
            break

    print(f"client={client_id}  scanned={scanned}  need_backfill={len(to_fix)}")
    for d, ct, md, mh in to_fix[:8]:
        print(f"  - {md.get('product_id'):<18} content.handle=None -> {mh!r}  ({(ct.get('title') or '')[:32]})")
    if len(to_fix) > 8:
        print(f"  ... and {len(to_fix) - 8} more")

    if not apply:
        print("  DRY RUN — no writes. Re-run with --apply to patch.")
        return len(to_fix), 0

    fixed = 0
    for d, ct, md, mh in to_fix:
        new_content = dict(ct)
        new_content["handle"] = mh
        if not new_content.get("created_at_ts"):
            ts = _iso_to_epoch(md.get("created_at") or ct.get("created_at"))
            if ts:
                new_content["created_at_ts"] = ts
        try:
            idx.upsert(documents=[{"id": d.id, "content": new_content, "metadata": dict(md)}])
            fixed += 1
        except Exception as e:  # noqa: BLE001 - report and continue
            print(f"  ! failed {d.id}: {e}")
    print(f"  APPLIED: patched {fixed}/{len(to_fix)} docs for {client_id}")
    return len(to_fix), fixed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client", action="append", required=True,
                    help="Client id (repeatable) whose Upstash index to backfill")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = ap.parse_args()

    url = os.environ.get("UPSTASH_SEARCH_REST_URL")
    token = os.environ.get("UPSTASH_SEARCH_REST_TOKEN")
    if not url or not token:
        raise SystemExit("UPSTASH_SEARCH_REST_URL and UPSTASH_SEARCH_REST_TOKEN must be set")

    client = Search(url=url, token=token)
    total_need = total_fixed = 0
    for cid in args.client:
        need, fixed = backfill_client(client, cid, apply=args.apply)
        total_need += need
        total_fixed += fixed
    print(f"\nTOTAL need_backfill={total_need}  patched={total_fixed}  apply={args.apply}")


if __name__ == "__main__":
    main()
