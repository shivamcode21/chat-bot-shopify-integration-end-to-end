"""Tests for rating/rating_count in the product branch of
build_entity_summary() (utils/context_helpers.py).

This is what the LLM actually sees for a *follow-up* question about a
product already in conversation context (e.g. "what's its rating?" after
the product was mentioned earlier in the same conversation) -- a separate
code path from the fresh tool-result shape covered in
test_tool_result_rating.py and test_product_card_rating.py. Without rating
surfaced here too, a follow-up question has no rating data available even
though a fresh tool call would.
"""

import pytest


def _summary():
    pytest.importorskip("pydantic")
    from fashion_bot.utils.context_helpers import build_entity_summary
    return build_entity_summary


def _full_data(**overrides):
    base = {
        "price": {"min": 999, "max": 999},
        "sizes_in_stock": ["S", "M"],
        "in_stock": True,
        "url": "https://shop.example.com/products/rated-tee",
    }
    base.update(overrides)
    return base


def test_includes_rating_when_present():
    build_entity_summary = _summary()
    summary = build_entity_summary("product", _full_data(rating=4.5, rating_count=2))
    assert summary["rating"] == 4.5
    assert summary["rating_count"] == 2


def test_omits_rating_when_absent():
    build_entity_summary = _summary()
    # No rating/rating_count on full_data at all -- the common/expected case
    # for an unrated product (see tool_factory._normalize_product /
    # utils.product_utils.extract_product_rating, which never set these keys
    # in the first place when there's no usable rating).
    summary = build_entity_summary("product", _full_data())
    assert "rating" not in summary
    assert "rating_count" not in summary


def test_existing_fields_unchanged_regardless_of_rating():
    """Baseline regression: the four pre-existing summary fields must be
    identical whether or not rating is present, for a product entity."""
    build_entity_summary = _summary()
    without_rating = build_entity_summary("product", _full_data())
    with_rating = build_entity_summary("product", _full_data(rating=4.5, rating_count=2))
    for key in ("price", "sizes_available", "in_stock", "product_link"):
        assert without_rating[key] == with_rating[key]


def test_non_product_entity_types_unaffected():
    """order/category/discount_code branches are untouched by this change."""
    build_entity_summary = _summary()
    order_summary = build_entity_summary("order", {"total_price": "999", "line_items": []})
    assert "rating" not in order_summary
