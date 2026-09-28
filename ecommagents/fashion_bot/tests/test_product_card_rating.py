"""Tests for product-card rating/count display (Judge.me metafields already
resident on the Upstash-sourced product dict -- no Judge.me call here).

``fashion_bot.websocket_chat`` pulls heavy deps, so guard with importorskip
(same pattern as test_carousel_candidate_set.py).
"""

import pytest


def _funcs():
    pytest.importorskip("pydantic")
    pytest.importorskip("fastapi")
    from fashion_bot.websocket_chat import (
        _extract_product_rating,
        format_product_for_carousel,
    )
    return _extract_product_rating, format_product_for_carousel


def _product(handle="rated-tee", **overrides):
    base = {
        "title": "Rated Tee",
        "handle": handle,
        "url": "https://shop.example.com/products/rated-tee",
        "image_url": "https://shop.example.com/img.jpg",
        "price": "999",
    }
    base.update(overrides)
    return base


# ---- _extract_product_rating ----

def test_extracts_valid_rating_and_count():
    extract, _ = _funcs()
    product = _product(metafield_attributes={
        "rating": '{"scale_min":"1.0","scale_max":"5.0","value":"4.6"}',
        "rating_count": "128",
    })
    result = extract(product)
    assert result == {"rating": 4.6, "rating_count": 128}


def test_zero_reviews_returns_none():
    extract, _ = _funcs()
    # No metafield_attributes at all -- the common/expected case for an
    # unreviewed product.
    assert extract(_product()) is None


def test_rating_count_zero_returns_none():
    extract, _ = _funcs()
    product = _product(metafield_attributes={
        "rating": '{"scale_min":"1.0","scale_max":"5.0","value":"5.0"}',
        "rating_count": "0",
    })
    assert extract(product) is None


def test_malformed_rating_json_returns_none_not_raises():
    extract, _ = _funcs()
    product = _product(metafield_attributes={
        "rating": "not-json",
        "rating_count": "5",
    })
    assert extract(product) is None


def test_non_dict_metafield_attributes_returns_none():
    extract, _ = _funcs()
    product = _product(metafield_attributes="unexpected-string")
    assert extract(product) is None


def test_missing_rating_count_returns_none():
    extract, _ = _funcs()
    product = _product(metafield_attributes={
        "rating": '{"scale_min":"1.0","scale_max":"5.0","value":"4.0"}',
    })
    assert extract(product) is None


# ---- format_product_for_carousel end-to-end ----

def test_carousel_card_includes_rating_when_present():
    _, fmt = _funcs()
    product = _product(metafield_attributes={
        "rating": '{"scale_min":"1.0","scale_max":"5.0","value":"4.6"}',
        "rating_count": "128",
    })
    card = fmt(product)
    assert card["rating"] == 4.6
    assert card["rating_count"] == 128


def test_carousel_card_omits_rating_for_zero_reviews():
    """0-review product -> card dict has no rating keys at all, i.e. the
    normal (pre-rating) card shape, per the product requirement."""
    _, fmt = _funcs()
    card = fmt(_product())
    assert "rating" not in card
    assert "rating_count" not in card
    # Sanity: still a normal, otherwise-complete card.
    assert card["title"] == "Rated Tee"
    assert card["handle"] == "rated-tee"


def test_carousel_card_never_breaks_on_malformed_rating_data():
    _, fmt = _funcs()
    product = _product(metafield_attributes={"rating": "garbage", "rating_count": "abc"})
    card = fmt(product)
    assert card is not None
    assert "rating" not in card
