"""Unit tests for ``ShopifyOrderAdapter._find_variant_by_size``.

Covers the production incident of 25 Aug 2026 (order for 7395918227): the
customer's cart already pinned the exact variant, but ``create_cart_order``
discarded that id and re-derived the variant from a free-text size label. The
store's own option value carried a stray double space ("Pack of 2  16g") while
the agent passed it single-spaced, so nothing matched and the customer was told
the order failed "due to a technical issue".

Two defences are tested here: resolving by variant id when the caller knows it,
and the whitespace-tolerant size fallback for when it does not.

Fully self-contained — no network, no live Shopify.
"""

import pytest

from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter


def _variant(vid, title, options, *, available=True):
    return {
        "id": f"gid://shopify/ProductVariant/{vid}",
        "title": title,
        "is_available": available,
        "selectedOptions": [{"name": name, "value": value} for name, value in options],
    }


# The store's real option values, double spaces and all.
PACK_PRODUCT = {
    "id": "gid://shopify/Product/9748974829881",
    "title": "Serum Pack",
    "variants": [
        _variant("50", "Pack of 1  8g", [("Size", "Pack of 1  8g")]),
        _variant("51", "Pack of 2  16g", [("Size", "Pack of 2  16g")]),
    ],
}


def _resolve(size, **kwargs):
    return ShopifyOrderAdapter._find_variant_by_size(PACK_PRODUCT, size, **kwargs)


# ── Resolution by variant id (the cart already knew which variant) ───────────

def test_variant_id_resolves_without_relying_on_size():
    result = _resolve("", requested_variant_id="51")
    assert result["success"] is True
    assert result["variant_id"] == "51"


def test_variant_id_accepts_shopify_gid_form():
    result = _resolve("", requested_variant_id="gid://shopify/ProductVariant/51")
    assert result["success"] is True
    assert result["variant_id"] == "51"


def test_variant_id_wins_over_a_mismatched_size_label():
    # The id is what the customer committed to in the cart; the label is retyped
    # by the agent and is the less trustworthy of the two.
    result = _resolve("Pack of 1 8g", requested_variant_id="51")
    assert result["success"] is True
    assert result["variant_id"] == "51"


def test_stale_variant_id_falls_back_to_size_matching():
    # An id that is not a variant of this product must not hard-fail the order
    # while a usable size label is still on the table.
    result = _resolve("Pack of 2 16g", requested_variant_id="99999")
    assert result["success"] is True
    assert result["variant_id"] == "51"


def test_variant_id_does_not_bypass_the_stock_gate():
    product = {
        "id": "gid://shopify/Product/1",
        "title": "Serum Pack",
        "variants": [
            _variant("50", "Pack of 1  8g", [("Size", "Pack of 1  8g")]),
            _variant("51", "Pack of 2  16g", [("Size", "Pack of 2  16g")], available=False),
        ],
    }
    result = ShopifyOrderAdapter._find_variant_by_size(product, "", requested_variant_id="51")
    assert result["success"] is False
    assert result["out_of_stock"] is True
    assert result["in_stock_sizes"] == ["Pack of 1  8g"]


# ── Size fallback: the incident itself ──────────────────────────────────────

@pytest.mark.parametrize("size", ["Pack of 2 16g", "Pack of 2  16g", "pack of 2   16g"])
def test_size_matching_tolerates_whitespace_variance(size):
    result = _resolve(size)
    assert result["success"] is True
    assert result["variant_id"] == "51"


def test_genuinely_unknown_size_still_fails_with_available_sizes():
    result = _resolve("Pack of 3 24g")
    assert result["success"] is False
    assert "Pack of 3 24g" in result["error"]
    assert "Pack of 2  16g" in result["error"]


def test_product_without_variants_fails_cleanly():
    result = ShopifyOrderAdapter._find_variant_by_size(
        {"id": "gid://shopify/Product/2", "title": "Empty", "variants": []}, "M"
    )
    assert result["success"] is False
    assert "No variants" in result["error"]


# ── CartOrderItem tolerates JSON-number ids and sizes ────────────────────────
# Variant ids are long digit strings and some sizes are bare numbers ("32"), so
# a model will sometimes emit them unquoted. Strict str validation would reject
# the whole create_cart_order call and drop the order — the exact failure mode
# this change exists to remove.

def test_cart_order_item_accepts_numeric_variant_id():
    from fashion_bot.tool_factory import CartOrderItem

    item = CartOrderItem(product_link="https://x/products/y", size="M", variant_id=51234567890)
    assert item.variant_id == "51234567890"


def test_cart_order_item_accepts_numeric_size():
    from fashion_bot.tool_factory import CartOrderItem

    item = CartOrderItem(product_link="https://x/products/y", size=32)
    assert item.size == "32"


def test_cart_order_item_variant_id_defaults_to_empty():
    from fashion_bot.tool_factory import CartOrderItem

    item = CartOrderItem(product_link="https://x/products/y", size="M")
    assert item.variant_id == ""
    assert item.quantity == 1
