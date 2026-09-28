"""Tests for rating/rating_count surfacing in the LLM-facing tool result
(``_normalize_product`` in ``tool_factory.py``) -- the piece that lets the
LLM mention a rating in plain text (WhatsApp, or a web chat text reply),
distinct from ``format_product_for_carousel`` in ``websocket_chat.py`` which
only feeds the visual web-widget card.

Judge.me metafields already resident on the product dict -- no Judge.me
call here.
"""

import pytest


def _funcs():
    pytest.importorskip("pydantic")
    pytest.importorskip("langchain_core")
    from fashion_bot.tool_factory import _extract_product_rating, _normalize_product
    return _extract_product_rating, _normalize_product


def _raw_product(**overrides):
    base = {
        "id": "gid://shopify/Product/123",
        "title": "Rated Tee",
        "handle": "rated-tee",
        "url": "https://shop.example.com/products/rated-tee",
    }
    base.update(overrides)
    return base


def test_normalize_product_surfaces_rating_and_count():
    _, normalize = _funcs()
    product = _raw_product(metafield_attributes={
        "rating": '{"scale_min":"1.0","scale_max":"5.0","value":"4.5"}',
        "rating_count": "2",
    })
    result = normalize(product)
    assert result["rating"] == 4.5
    assert result["rating_count"] == 2


def test_normalize_product_omits_rating_when_unrated():
    _, normalize = _funcs()
    # No metafield_attributes at all -- the common/expected case for an
    # unreviewed product. Must not appear as a key at all (not None, not 0)
    # so the LLM's tool-result shape matches its docstring ("Absent when
    # unrated") rather than looking like a real-but-empty value.
    result = normalize(_raw_product())
    assert "rating" not in result
    assert "rating_count" not in result


def test_normalize_product_omits_rating_when_count_zero():
    _, normalize = _funcs()
    product = _raw_product(metafield_attributes={
        "rating": '{"scale_min":"1.0","scale_max":"5.0","value":"5.0"}',
        "rating_count": "0",
    })
    result = normalize(product)
    assert "rating" not in result
    assert "rating_count" not in result


def test_extract_product_rating_matches_carousel_helper_behavior():
    """tool_factory._extract_product_rating is an import alias for the
    shared utils/product_utils.extract_product_rating -- also used by
    websocket_chat.format_product_for_carousel. See
    test_product_utils_rating.py for coverage of the extraction logic
    itself (scale normalization, lenient count parsing); this just pins
    the alias to the same input/output shape callers here rely on."""
    extract, _ = _funcs()
    product = _raw_product(metafield_attributes={
        "rating": '{"scale_min":"1.0","scale_max":"5.0","value":"4.6"}',
        "rating_count": "128",
    })
    assert extract(product) == {"rating": 4.6, "rating_count": 128}


def test_extract_product_rating_malformed_json_fails_open():
    extract, _ = _funcs()
    product = _raw_product(metafield_attributes={
        "rating": "not-json",
        "rating_count": "5",
    })
    assert extract(product) is None
