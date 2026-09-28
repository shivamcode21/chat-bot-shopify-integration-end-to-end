#!/usr/bin/env python3
"""
Seed / update the 'order_update_rules' config in the client_configs table.

These rules are the SINGLE SOURCE OF TRUTH for which order-update types
(name, email, address, phone, size, product) are allowed based on the
current order status.  They are loaded at runtime by:

    fashion_bot.utils.utils.get_order_update_rules(client_id)

Resolution order:  Redis cache -> client_configs DB -> hardcoded fallback.

Usage:
    # Preview what would be written (dry run):
    python scripts/seed_order_update_rules.py --dry-run

    # Seed for the default client:
    python scripts/seed_order_update_rules.py

    # Seed for a specific client:
    python scripts/seed_order_update_rules.py --client-id YOUR_CLIENT_UUID

    # Seed with a custom JSON file (overrides the built-in rules):
    python scripts/seed_order_update_rules.py --from-file rules.json
"""

import os
import sys
import json
import argparse

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()


# ──────────────────────────────────────────────────────────────────────
#  DEFAULT RULES
#  These are the GV defaults.  Other clients may override via --from-file
#  or by editing the DB row directly.
# ──────────────────────────────────────────────────────────────────────

DEFAULT_ORDER_UPDATE_RULES = {
    "name": {
        "direct_update_statuses": [
            "NEW", "PICKUP", "NOT_YET_DISPATCHED", "PICKUP_EXCEPTION",
            "PICKUP_RESCHEDULED", "IN_TRANSIT", "SHIPPED", "DISPATCHED",
            "OUT_FOR_DELIVERY",
        ],
        "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"],
        "blocked_message": "Name changes cannot be made for delivered or cancelled orders.",
    },
    "email": {
        "direct_update_statuses": [
            "NEW", "PICKUP", "NOT_YET_DISPATCHED", "PICKUP_EXCEPTION",
            "PICKUP_RESCHEDULED", "IN_TRANSIT", "SHIPPED", "DISPATCHED",
            "OUT_FOR_DELIVERY",
        ],
        "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"],
        "blocked_message": "Email changes cannot be made for delivered or cancelled orders.",
    },
    "address": {
        "direct_update_statuses": ["NEW"],
        "escalate_statuses": [
            "PICKUP", "NOT_YET_DISPATCHED", "PICKUP_EXCEPTION", "PICKUP_RESCHEDULED",
        ],
        "email_partner_statuses": [
            "IN_TRANSIT", "SHIPPED", "DISPATCHED", "OUT_FOR_DELIVERY",
        ],
        "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"],
        "blocked_message": "Address changes cannot be made for delivered or cancelled orders.",
    },
    "phone": {
        "direct_update_statuses": ["NEW"],
        "escalate_statuses": [
            "PICKUP", "NOT_YET_DISPATCHED", "PICKUP_EXCEPTION", "PICKUP_RESCHEDULED",
        ],
        "email_partner_statuses": [
            "IN_TRANSIT", "SHIPPED", "DISPATCHED", "OUT_FOR_DELIVERY",
        ],
        "add_alternate_phone": True,
        "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"],
        "blocked_message": "Phone changes cannot be made for delivered or cancelled orders.",
    },
    "size": {
        "direct_update_statuses": ["NEW"],
        "escalate_statuses": [
            "PICKUP", "NOT_YET_DISPATCHED", "PICKUP_EXCEPTION", "PICKUP_RESCHEDULED",
        ],
        "email_partner_statuses": [
            "IN_TRANSIT", "SHIPPED", "DISPATCHED", "OUT_FOR_DELIVERY",
        ],
        "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"],
        "blocked_message": "Size changes cannot be made for delivered or cancelled orders.",
    },
    "product": {
        "direct_update_statuses": ["NEW"],
        "blocked_message": "Product changes can only be made before the order is dispatched.",
    },
}


# ──────────────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────────────

def get_client_id(override=None):
    """Resolve client_id from arg, env, or DB default."""
    if override:
        return override
    env_cid = os.environ.get("CLIENT_ID")
    if env_cid:
        return env_cid
    try:
        from fashion_bot.config_manager import get_default_client_id
        return get_default_client_id()
    except Exception as e:
        print(f"Could not resolve client_id: {e}")
        print("   Pass --client-id explicitly.")
        sys.exit(1)


def upsert_rules(client_id, rules, dry_run=False):
    """Insert or update order_update_rules in client_configs."""
    from fashion_bot.database_manager import get_postgres_connection

    config_key = "order_update_rules"
    rules_json = json.dumps(rules, indent=2)

    if dry_run:
        print(f"\n{'='*60}")
        print(f"DRY RUN - would upsert into client_configs:")
        print(f"  client_id  = {client_id}")
        print(f"  config_key = {config_key}")
        print(f"  value size = {len(rules_json)} chars")
        print(f"{'='*60}")
        print(json.dumps(rules, indent=2))
        return

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            # Check if row exists for this client + key
            cur.execute(
                "SELECT id FROM client_configs WHERE client_id = %s AND config_key = %s",
                (client_id, config_key),
            )
            existing = cur.fetchone()

            if existing:
                cur.execute(
                    """
                    UPDATE client_configs
                    SET config_value = %s::jsonb
                    WHERE client_id = %s AND config_key = %s
                    """,
                    (rules_json, client_id, config_key),
                )
                print(f"Updated 'order_update_rules' for client '{client_id}' ({len(rules_json)} chars)")
            else:
                cur.execute(
                    """
                    INSERT INTO client_configs (client_id, config_key, config_value)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (client_id, config_key, rules_json),
                )
                print(f"Inserted 'order_update_rules' for client '{client_id}' ({len(rules_json)} chars)")

            conn.commit()


def invalidate_cache(client_id):
    """Clear Redis cache so the new rules are picked up immediately."""
    try:
        from fashion_bot.utils.utils import _get_redis_client
        rc = _get_redis_client()
        if rc:
            cache_key = f"order_update_rules:{client_id}"
            rc.delete(cache_key)
            # Also clear the "default" key in case it was cached without client_id
            rc.delete("order_update_rules:default")
            print(f"Invalidated Redis cache for '{cache_key}' + 'order_update_rules:default'")
        else:
            print("Redis not available - cache will expire naturally (10 min TTL)")
    except Exception as e:
        print(f"Could not invalidate cache: {e}")


def verify_rules(client_id):
    """Read back from DB to confirm the rules were saved correctly."""
    from fashion_bot.config_manager import get_config
    raw = get_config("order_update_rules", client_id=client_id)
    if raw:
        rules = json.loads(raw) if isinstance(raw, str) else raw
        update_types = list(rules.keys())
        print(f"Verified: DB has rules for update types: {update_types}")
        for ut, cfg in rules.items():
            blocked = cfg.get("blocked_statuses", [])
            direct = cfg.get("direct_update_statuses", [])
            escalate = cfg.get("escalate_statuses", [])
            email_p = cfg.get("email_partner_statuses", [])
            print(f"  {ut:10s}  direct={direct}  escalate={escalate}  email_partner={email_p}  blocked={blocked}")
        return True
    else:
        print("FAILED: Could not read back rules from DB!")
        return False


# ──────────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Seed or update order_update_rules in client_configs"
    )
    parser.add_argument(
        "--client-id", type=str, default=None,
        help="Client UUID (defaults to system default)",
    )
    parser.add_argument(
        "--from-file", type=str, default=None,
        help="Path to a JSON file with custom rules (overrides built-in defaults)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print rules without writing to DB",
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="Only read and display the current rules from DB (no writes)",
    )
    args = parser.parse_args()

    client_id = get_client_id(args.client_id)
    print(f"Client ID: {client_id}")

    # Verify-only mode
    if args.verify_only:
        verify_rules(client_id)
        return

    # Determine which rules to use
    if args.from_file:
        print(f"Loading rules from file: {args.from_file}")
        with open(args.from_file, "r") as f:
            rules = json.load(f)
        print(f"Loaded {len(rules)} update types from file: {list(rules.keys())}")
    else:
        rules = DEFAULT_ORDER_UPDATE_RULES
        print(f"Using built-in default rules ({len(rules)} update types: {list(rules.keys())})")

    # Upsert
    upsert_rules(client_id, rules, dry_run=args.dry_run)

    # Invalidate cache + verify
    if not args.dry_run:
        invalidate_cache(client_id)
        print()
        verify_rules(client_id)
        print("\nDone! Rules will be used on the next request.")
    else:
        print("\nDry run complete. No changes were made.")


if __name__ == "__main__":
    main()

