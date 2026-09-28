"""
Cancel all open Shopify orders for a given client (staging use only).

Usage:
    cd /Users/shivammehrotra/git-bot/ecommagents/fashion_bot
    PYTHONPATH=. python3 scripts/cancel_all_orders.py --client-id c3ffcb1b-afb9-4ca4-8746-a06698bec870

Optional flags:
    --dry-run          Print what would be cancelled without actually cancelling
    --status           Shopify order status filter (default: open). Options: open|closed|any
    --reason           Cancellation reason sent to Shopify (default: other)
                       Options: customer|fraud|inventory|declined|other
    --refund           Issue a refund on cancellation (default: false)
    --limit            Max orders to cancel in one run (default: unlimited)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("cancel_all_orders")

# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap env (same as backfill script)
# ──────────────────────────────────────────────────────────────────────────────
async def _load_shopify_creds(client_id: str) -> dict[str, str]:
    from fashion_bot.config_manager import aget_shopify_config

    cfg = await aget_shopify_config(client_id=client_id)
    if not cfg:
        raise SystemExit(f"No shopify_details found for client_id={client_id}")

    shop_domain = (
        cfg.get("SHOPIFY_DOMAIN")
        or cfg.get("shop_url")
        or cfg.get("shopify_domain")
    )
    access_token = (
        cfg.get("SHOPIFY_ACCESS_TOKEN")
        or cfg.get("access_token")
    )
    if not shop_domain or not access_token:
        raise SystemExit(
            f"shopify_details for client_id={client_id} is missing "
            "SHOPIFY_DOMAIN or SHOPIFY_ACCESS_TOKEN"
        )

    # Normalise: strip scheme, trailing slash
    shop_domain = shop_domain.replace("https://", "").replace("http://", "").rstrip("/")
    return {"shop_domain": shop_domain, "access_token": access_token}


# ──────────────────────────────────────────────────────────────────────────────
# Shopify REST helpers
# ──────────────────────────────────────────────────────────────────────────────
API_VERSION = "2024-04"


def _base_url(shop_domain: str) -> str:
    return f"https://{shop_domain}/admin/api/{API_VERSION}"


def _headers(access_token: str) -> dict[str, str]:
    return {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }


async def _fetch_orders_page(
    client: httpx.AsyncClient,
    shop_domain: str,
    access_token: str,
    *,
    status: str,
    page_info: str | None,
    limit: int = 250,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return (orders, next_page_info)."""
    params: dict[str, Any] = {"limit": limit, "status": status}
    if page_info:
        params["page_info"] = page_info
    else:
        # First page — include fields we need
        params["fields"] = "id,name,financial_status,fulfillment_status,cancelled_at"

    url = f"{_base_url(shop_domain)}/orders.json"
    resp = await client.get(url, headers=_headers(access_token), params=params)
    resp.raise_for_status()

    orders = resp.json().get("orders", [])

    # Parse Link header for next page
    next_page_info: str | None = None
    link_header = resp.headers.get("Link", "")
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            # Extract page_info= value
            url_part = part.split(";")[0].strip().strip("<>")
            for qp in url_part.split("&"):
                if qp.startswith("page_info="):
                    next_page_info = qp.split("=", 1)[1]
                    break

    return orders, next_page_info


async def _cancel_order(
    client: httpx.AsyncClient,
    shop_domain: str,
    access_token: str,
    order_id: int,
    *,
    reason: str,
    refund: bool,
) -> dict[str, Any]:
    url = f"{_base_url(shop_domain)}/orders/{order_id}/cancel.json"
    payload = {"reason": reason, "refund": refund, "note": "bloomerce-cancelled-orders"}
    resp = await client.post(url, headers=_headers(access_token), json=payload, timeout=30)
    return {"status_code": resp.status_code, "body": resp.json()}


# ──────────────────────────────────────────────────────────────────────────────
# Main logic
# ──────────────────────────────────────────────────────────────────────────────
async def run(
    client_id: str,
    *,
    dry_run: bool,
    status: str,
    reason: str,
    refund: bool,
    max_limit: int | None,
) -> None:
    creds = await _load_shopify_creds(client_id)
    shop_domain = creds["shop_domain"]
    access_token = creds["access_token"]

    logger.info(
        "Shop: %s | status filter: %s | dry_run: %s | refund: %s | reason: %s",
        shop_domain, status, dry_run, refund, reason,
    )

    cancelled = 0
    failed = 0
    skipped = 0
    page_info: str | None = None
    page_num = 0

    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            page_num += 1
            orders, next_page_info = await _fetch_orders_page(
                http, shop_domain, access_token,
                status=status, page_info=page_info,
            )
            if not orders:
                logger.info("No more orders on page %d — done.", page_num)
                break

            logger.info("Page %d: fetched %d orders", page_num, len(orders))

            for order in orders:
                if max_limit and (cancelled + failed) >= max_limit:
                    logger.info("Reached --limit %d — stopping.", max_limit)
                    break

                order_id = order["id"]
                order_name = order.get("name", f"#{order_id}")

                # Skip already-cancelled orders
                if order.get("cancelled_at"):
                    logger.info("  SKIP %s — already cancelled", order_name)
                    skipped += 1
                    continue

                if dry_run:
                    logger.info("  [DRY-RUN] Would cancel %s (id=%s)", order_name, order_id)
                    cancelled += 1
                    continue

                result = await _cancel_order(
                    http, shop_domain, access_token, order_id,
                    reason=reason, refund=refund,
                )
                sc = result["status_code"]
                if sc in (200, 201):
                    logger.info("  ✅ Cancelled %s (id=%s)", order_name, order_id)
                    cancelled += 1
                else:
                    logger.warning(
                        "  ❌ Failed %s (id=%s) status=%s body=%s",
                        order_name, order_id, sc, result["body"],
                    )
                    failed += 1

                # Polite rate limiting: Shopify REST = 2 req/s bucket
                await asyncio.sleep(0.5)

            if max_limit and (cancelled + failed) >= max_limit:
                break

            if not next_page_info:
                break
            page_info = next_page_info

    logger.info(
        "Done. cancelled=%d failed=%d skipped=%d dry_run=%s",
        cancelled, failed, skipped, dry_run,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Cancel all Shopify orders for a client (staging only)")
    parser.add_argument("--client-id", required=True, help="client_id UUID")
    parser.add_argument("--dry-run", action="store_true", help="Print without cancelling")
    parser.add_argument("--status", default="open", choices=["open", "closed", "any"],
                        help="Shopify order status filter (default: open)")
    parser.add_argument("--reason", default="other",
                        choices=["customer", "fraud", "inventory", "declined", "other"],
                        help="Cancellation reason (default: other)")
    parser.add_argument("--refund", action="store_true", help="Issue refund on cancel")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max orders to process (default: all)")
    args = parser.parse_args()

    asyncio.run(run(
        args.client_id,
        dry_run=args.dry_run,
        status=args.status,
        reason=args.reason,
        refund=args.refund,
        max_limit=args.limit,
    ))


if __name__ == "__main__":
    main()
