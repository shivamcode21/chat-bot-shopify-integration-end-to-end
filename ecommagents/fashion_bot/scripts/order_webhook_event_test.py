#!/usr/bin/env python3
"""
Post Shopify order webhook payloads to /order/webhook for template event-key testing.

Each event key is driven by financial_status + fulfillment_status (and cancelled_at for VOIDED).
See EVENT_VARIANTS below for the exact field changes on top of the base order JSON.

Usage:
  python fashion_bot/scripts/order_webhook_event_test.py FULFILLED
  python fashion_bot/scripts/order_webhook_event_test.py PAID_UNFULFILLED --base-url http://localhost:8000
  python fashion_bot/scripts/order_webhook_event_test.py --list
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Base payload — Iconic order #45209 (trimmed only where noted; structure preserved).
BASE_ORDER = {
    "id": 7174866600239,
    "admin_graphql_api_id": "gid://shopify/Order/7174866600239",
    "app_id": 76708773889,
    "confirmation_number": "6N5NNHKE8",
    "confirmed": True,
    "name": "#45209",
    "order_number": 45209,
    "created_at": "2026-06-23T18:46:18+05:30",
    "updated_at": "2026-06-24T14:38:34+05:30",
    "closed_at": None,
    "processed_at": "2026-06-23T18:46:18+05:30",
    "financial_status": "paid",
    "fulfillment_status": "fulfilled",
    "currency": "INR",
    "presentment_currency": "INR",
    "email": "munish@stoln.in",
    "phone": "+919811087654",
    "contact_email": "munish@stoln.in",
    "subtotal_price": "2659.05",
    "total_price": "2659.05",
    "total_discounts": "139.95",
    "total_tax": "405.62",
    "total_weight": 1025,
    "total_outstanding": "0.00",
    "taxes_included": True,
    "test": False,
    "tags": "Cards, GoKwik, PREPAID-DISCOUNT",
    "payment_gateway_names": ["Gokwik Cards"],
    "note_attributes": [
        {"name": "gokwik_cid", "value": "7553afea-694d-4350-8b83-d19449dc2e3c"},
        {"name": "cart_token", "value": "hWNDfkapA6pQkqAMSRobyShw?key=31b0cc94520aca259f112e9ad0a79699"},
        {"name": "customer_ip", "value": "2401:4900:1c66:385b:d4d3:5503:35a1:c3ac"},
        {"name": "deliver_order_count", "value": "23"},
        {"name": "utm_source", "value": "FB_Paid"},
        {"name": "utm_campaign", "value": "120248714293830121"},
        {"name": "PREPAID-DISCOUNT", "value": "139.95"},
        {"name": "GoKwik_Payment_ID", "value": "KWIKMLMH8U840549312MP"},
        {"name": "Payment_Provider_Name", "value": "easebuzz"},
        {"name": "Payment_Provider_Payment_ID", "value": "E260623110U21F"},
    ],
    "discount_codes": [
        {"code": "PREPAID-DISCOUNT", "amount": "139.95", "type": "fixed_amount"}
    ],
    "billing_address": {
        "first_name": "Munish",
        "last_name": "Sahgal",
        "address1": "J 7 Saket Ground Floor",
        "city": "SOUTH",
        "zip": "110017",
        "province": "Delhi",
        "country": "India",
        "country_code": "IN",
        "province_code": "DL",
        "phone": "9811087654",
    },
    "shipping_address": {
        "first_name": "Munish",
        "last_name": "Sahgal",
        "address1": "J 7 Saket Ground Floor",
        "city": "SOUTH",
        "zip": "110017",
        "province": "Delhi",
        "country": "India",
        "country_code": "IN",
        "province_code": "DL",
        "phone": "9811087654",
    },
    "customer": {
        "id": 7363246260527,
        "first_name": "Munish",
        "last_name": "Sahgal",
        "email": "munish@stoln.in",
        "currency": "INR",
        "state": "disabled",
        "verified_email": True,
        "created_at": "2023-08-13T17:09:55+05:30",
        "updated_at": "2026-06-23T18:46:19+05:30",
    },
    "line_items": [
        {
            "id": 17145653952815,
            "name": "Iconic Men Cream Textured Resort Fit Shirt - XL",
            "title": "Iconic Men Cream Textured Resort Fit Shirt",
            "variant_title": "XL",
            "sku": "8909406080628",
            "product_id": 14690375434543,
            "variant_id": 56524013338927,
            "quantity": 1,
            "price": "2799.00",
            "total_discount": "0.00",
            "fulfillment_status": "fulfilled",
            "vendor": "Iconic",
            "taxable": True,
            "requires_shipping": True,
            "grams": 1025,
        }
    ],
    "fulfillments": [
        {
            "id": 6360142938415,
            "name": "#45209.1",
            "status": "success",
            "shipment_status": "in_transit",
            "tracking_company": "Delhivery",
            "tracking_number": "5197611494533",
            "tracking_url": "https://www.delhivery.com/track/package/5197611494533",
            "created_at": "2026-06-24T12:41:03+05:30",
            "updated_at": "2026-06-24T14:38:34+05:30",
        }
    ],
    "shipping_lines": [{"title": "Free Shipping", "code": "Free Shipping", "price": "0.00"}],
    "discount_applications": [
        {
            "type": "manual",
            "title": "PREPAID-DISCOUNT",
            "value": "139.95",
            "value_type": "fixed_amount",
            "target_type": "line_item",
        }
    ],
    "payment_terms": None,
    "refunds": [],
    "returns": [],
    "cancelled_at": None,
}

# Maps event_key → fields to merge onto BASE_ORDER.
# Resolution order in event_config.aresolve_event_key:
#   1) fulfillment_status (upper)  e.g. FULFILLED, VOIDED
#   2) financial_status (upper)    e.g. PENDING, PAID
#   3) PAYMENT_{financial}_{fulfillment}
#   4) {financial}_{fulfillment}
EVENT_VARIANTS: dict[str, dict] = {
    "FULFILLED": {
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "cancelled_at": None,
        "closed_at": "2026-06-24T12:41:03+05:30",
        # keep fulfillments + line_items.fulfillment_status = fulfilled
    },
    "VOIDED": {
        "financial_status": "voided",
        "fulfillment_status": "unfulfilled",
        "cancelled_at": "2026-06-24T15:00:00+05:30",
        "closed_at": "2026-06-24T15:00:00+05:30",
        "fulfillments": [],
        "line_items": [
            {**BASE_ORDER["line_items"][0], "fulfillment_status": None}
        ],
    },
    "PAID_UNFULFILLED": {
        "financial_status": "paid",
        "fulfillment_status": None,
        "cancelled_at": None,
        "closed_at": None,
        "fulfillments": [],
        "line_items": [
            {**BASE_ORDER["line_items"][0], "fulfillment_status": None}
        ],
    },
    "PARTIALLY_PAID_UNFULFILLED": {
        "financial_status": "partially_paid",
        "fulfillment_status": None,
        "cancelled_at": None,
        "closed_at": None,
        "fulfillments": [],
        "line_items": [
            {**BASE_ORDER["line_items"][0], "fulfillment_status": None}
        ],
    },
    "PAYMENT_PENDING_UNFULFILLED": {
        "financial_status": "pending",
        "fulfillment_status": None,
        "cancelled_at": None,
        "closed_at": None,
        "fulfillments": [],
        "line_items": [
            {**BASE_ORDER["line_items"][0], "fulfillment_status": None}
        ],
    },
    # PAYMENT_PENDING: resolved only when phone is in client_whitelisted_numbers (shopify)
    # and the base key would have been PAYMENT_PENDING_UNFULFILLED (demo override in event_processor).
    "PAYMENT_PENDING": {
        "financial_status": "pending",
        "fulfillment_status": None,
        "cancelled_at": None,
        "closed_at": None,
        "fulfillments": [],
        "line_items": [
            {**BASE_ORDER["line_items"][0], "fulfillment_status": None}
        ],
    },
}

DEFAULT_SHOP_DOMAIN = "iconic-india.myshopify.com"
DEFAULT_TOPIC = "orders/updated"


def build_payload(event_key: str) -> dict:
    if event_key not in EVENT_VARIANTS:
        raise ValueError(f"Unknown event_key={event_key!r}. Choose from: {list(EVENT_VARIANTS)}")
    payload = copy.deepcopy(BASE_ORDER)
    payload.update(EVENT_VARIANTS[event_key])
    payload["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return payload


def post_webhook(
    event_key: str,
    *,
    base_url: str,
    shop_domain: str,
    topic: str,
    dry_run: bool,
) -> dict:
    payload = build_payload(event_key)
    url = f"{base_url.rstrip('/')}/order/webhook"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Topic": topic,
        "X-Shopify-Shop-Domain": shop_domain,
        "X-Shopify-Webhook-Id": f"test-{event_key.lower()}-{int(datetime.now().timestamp())}",
        "X-Shopify-Event-Id": f"evt-{event_key.lower()}",
    }

    print(f"\n=== Event key target: {event_key} ===")
    print(f"financial_status   : {payload.get('financial_status')}")
    print(f"fulfillment_status : {payload.get('fulfillment_status')}")
    print(f"cancelled_at       : {payload.get('cancelled_at')}")
    print(f"POST {url}")
    print(f"X-Shopify-Shop-Domain: {shop_domain}")
    print(f"X-Shopify-Topic      : {topic}")

    if dry_run:
        print("\n[dry-run] payload (first 800 chars):")
        print(json.dumps(payload, indent=2)[:800])
        return {"dry_run": True, "event_key": event_key}

    with httpx.Client(timeout=120.0) as client:
        resp = client.post(url, json=payload, headers=headers)
    print(f"\nHTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}
    print(json.dumps(body, indent=2))
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description="Test /order/webhook event keys")
    parser.add_argument(
        "event_key",
        nargs="?",
        choices=list(EVENT_VARIANTS.keys()),
        help="shopify template event_key to trigger",
    )
    parser.add_argument("--list", action="store_true", help="list event keys and field mapping")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--shop-domain", default=DEFAULT_SHOP_DOMAIN)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--dry-run", action="store_true", help="print payload only, no HTTP")
    parser.add_argument("--save", type=Path, help="write payload JSON to this file")
    args = parser.parse_args()

    if args.list:
        print("Event keys and required Shopify fields:\n")
        for key, variant in EVENT_VARIANTS.items():
            print(f"  {key}")
            print(f"    financial_status   = {variant.get('financial_status')!r}")
            print(f"    fulfillment_status = {variant.get('fulfillment_status')!r}")
            print(f"    cancelled_at       = {variant.get('cancelled_at')!r}")
            if key == "PAYMENT_PENDING":
                print("    note: needs whitelisted test phone for PAYMENT_PENDING override")
            print()
        return 0

    if not args.event_key:
        parser.error("event_key required (or use --list)")

    if args.save:
        payload = build_payload(args.event_key)
        args.save.write_text(json.dumps(payload, indent=2))
        print(f"Wrote {args.save}")

    post_webhook(
        args.event_key,
        base_url=args.base_url,
        shop_domain=args.shop_domain,
        topic=args.topic,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
