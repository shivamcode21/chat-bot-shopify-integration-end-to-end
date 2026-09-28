"""
Canonical Shopify order tags applied by Bloomerce bot operations.

All code that reads or writes these tags must import from here — never
use bare string literals.  This makes tag names grep-able, rename-safe,
and self-documenting.

Usage
-----
    from fashion_bot.shopify.order_tags import OrderTag

    # Pass to aclone_order / additional_tags lists:
    additional_tags=[OrderTag.SIZE_CHANGE_CLONED, OrderTag.BLOOMERCE_UPDATED]

    # Check membership against Shopify tags string/list:
    if OrderTag.BLOOMERCE_CREATED in tags:
        ...

Because ``OrderTag`` inherits from ``str``, every member IS a string —
no ``.value`` call needed.  Enum members compare equal to their string
counterpart and are directly JSON-serialisable.
"""
from enum import Enum


class OrderTag(str, Enum):
    """Shopify order tags applied by Bloomerce operations.

    Each tag documents the operation that caused it so downstream
    reporting, webhooks, and analytics can reliably identify bot-touched
    orders.
    """

    # ── Origin tags ────────────────────────────────────────────────────
    # Applied to NEW orders created directly by the bot.
    BLOOMERCE_CREATED = "BLOOMERCE_CREATED"

    # Applied to orders that are the result of a clone/recreate operation
    # (size change, product change, cancel-and-recreate address update).
    BLOOMERCE_UPDATED = "BLOOMERCE_UPDATED"

    # ── Operation-specific tags ────────────────────────────────────────
    # Cloned order replacing the original after a size / variant change.
    SIZE_CHANGE_CLONED = "SIZE_CHANGE_CLONED"

    # Cloned order replacing the original after a product (SKU) change.
    PRODUCT_CHANGE_CLONED = "PRODUCT_CHANGE_CLONED"

    # Cloned order replacing the original via the cancel-and-recreate
    # address / phone / email / name update strategy.
    CANCEL_AND_RECREATE = "CANCEL_AND_RECREATE"

    # Applied when the bot performs an address / phone / email update
    # in-place (without cloning — i.e. the legacy / escalate path).
    ORDER_ASSISTANCE = "ORDER_ASSISTANCE"

    # Exactly-once counting tag: applied to one order per update operation
    # so ``count(BLOOMERCE_EDITED)`` = total agent-assisted updates.
    # Clone flows: stamped on the OLD (cancelled) order only.
    # In-place edits: stamped on the edited order.
    BLOOMERCE_EDITED = "BLOOMERCE_EDITED"

    # Applied to the result dict (tags_added field) when the customer
    # requested a product change that triggered a cancel-and-recreate.
    USER_REQUESTED_PRODUCT_CHANGE = "USER_REQUESTED_PRODUCT_CHANGE"


# ── Payment-reference tag prefixes ─────────────────────────────────────────
# Dynamic per-order values, so these can't be OrderTag enum members. Applied to
# a cloned order to pin it back to the ORIGINAL order's captured payment so
# finance/ops can reconcile. All share the "orig_" namespace; the Shopify
# transaction id uses a fixed key, while gateway references preserve their
# original note_attribute key name — e.g. "orig_txn_id:8404506214722",
# "orig_PayU_txn_id:29092610009". Kept here (not as bare string literals) so the
# tags stay grep-able and rename-safe.
PAYMENT_REFERENCE_TAG_PREFIX = "orig_"
PAYMENT_TXN_TAG_PREFIX = PAYMENT_REFERENCE_TAG_PREFIX + "txn_id:"  # Shopify OrderTransaction.id
