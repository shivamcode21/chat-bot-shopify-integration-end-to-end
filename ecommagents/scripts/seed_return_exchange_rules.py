"""Seed sanitized Return Prime return/exchange rules into client_configs.

Usage:
    python scripts/seed_return_exchange_rules.py <client_id>

The rules are copied from the merchant's Return Prime settings and normalized
into a small JSON policy consumed by ``fashion_bot.return_partners.rules``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "fashion_bot"))

from fashion_bot.database_manager import get_postgres_connection  # noqa: E402
from fashion_bot.return_partners.rules import RETURN_EXCHANGE_RULES_CONFIG_KEY  # noqa: E402


SANITIZED_RETURN_EXCHANGE_RULES = {
    "version": 1,
    "source": "return_prime_settings_paste",
    "return": {
        "enabled": True,
        "window_days": 3,
        "require_delivered": True,
        "match_mode": "any",
        "blocked_product_tags": ["Winter Drop", "winter"],
        "blocked_discount_codes": [],
        "blocked_order_created_between": None,
        "customer_message": (
            "This order is not eligible for return based on the store's "
            "configured return policy."
        ),
    },
    "exchange": {
        "enabled": True,
        "window_days": 7,
        "require_delivered": True,
        "match_mode": "any",
        "blocked_product_tags": ["Winter Drop", "winter"],
        "blocked_discount_codes": [],
        "blocked_order_created_between": None,
        "customer_message": (
            "This order is not eligible for exchange based on the store's "
            "configured exchange policy."
        ),
    },
    "exchange_settings": {
        "allow_out_of_stock_exchange": False,
        "exchange_order_tags": [],
        "hold_exchange_orders_on_shopify": False,
        "allow_exchange_with_any_one_product": True,
        "allow_multiple_exchange_products": False,
        "discount_carry_forward": {
            "reuse_discount_amount": False,
            "reuse_discount_percentage": False,
            "eligible_coupon_codes": [],
        },
        "do_not_refund_same_product_variant_price_difference": False,
        "exchange_order_suffix": "-EXC",
    },
    "payments_and_fees": {
        "capture_price_difference": False,
        "capture_return_fee": False,
        "return_fee_rules": [],
        "exchange_fee_rules": [],
    },
    "multiple_item_returns": {"enabled": False},
    "automation": {
        "auto_receive_requests": False,
        "reopen_cancelled_requests_after_window_expiry": False,
        "auto_archive_after_refund_or_exchange_created": False,
        "auto_refund_additional_payment_for_rejected_requests": False,
        "update_shopify_inventory": True,
        "distribute_bxgy_discounts": False,
        "mark_cod_orders_refunded_on_shopify": False,
        "discount_code_expiry_days": 0,
    },
    "gift_returns": {
        "enabled": False,
        "refund_mode": "existing_gift_card",
    },
    "green_return_rules": [],
    "notes": [
        "Only enforceable validation rules are used by code today: window_days, "
        "require_delivered, blocked_product_tags, and window disabled when set to 0.",
        "Operational settings are stored for future Return Prime/Shopify parity.",
    ],
}


def upsert_return_exchange_rules(client_id: str) -> None:
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO client_configs (client_id, config_key, config_value)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (client_id, config_key) DO UPDATE
                    SET config_value = EXCLUDED.config_value
                """,
                (
                    client_id,
                    RETURN_EXCHANGE_RULES_CONFIG_KEY,
                    json.dumps(SANITIZED_RETURN_EXCHANGE_RULES),
                ),
            )
        conn.commit()


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/seed_return_exchange_rules.py <client_id>")
        return 2
    client_id = sys.argv[1].strip()
    if not client_id:
        print("client_id is required")
        return 2
    upsert_return_exchange_rules(client_id)
    print(
        f"Upserted {RETURN_EXCHANGE_RULES_CONFIG_KEY} for client_id={client_id} "
        f"with return_window=3 exchange_window=7"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
