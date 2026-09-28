"""Tests for the shared rating/count extraction utility
(``extract_product_rating`` in ``utils/product_utils.py``), used by both
``format_product_for_carousel`` (web-widget card) and ``_normalize_product``
(LLM-facing tool result) -- see test_product_card_rating.py and
test_tool_result_rating.py for coverage at each of those call sites.

Judge.me metafields already resident on the product dict -- no Judge.me
call here.
"""

from fashion_bot.utils.product_utils import extract_product_rating


def _product(**metafield_attributes):
    return {"title": "Rated Tee", "handle": "rated-tee", "metafield_attributes": metafield_attributes}


# ---- Common 1-5 scale (the only shape seen from Judge.me in practice) ----

def test_standard_1_to_5_scale():
    product = _product(
        rating='{"scale_min":"1.0","scale_max":"5.0","value":"4.6"}',
        rating_count="128",
    )
    assert extract_product_rating(product) == {"rating": 4.6, "rating_count": 128}


# ---- Non-1-5 scale normalization ----

def test_normalizes_0_to_10_scale_onto_1_to_5():
    # value=8 on a 0-10 scale -> 1 + (8-0)*4/10 = 4.2
    product = _product(
        rating='{"scale_min":"0","scale_max":"10","value":"8"}',
        rating_count="10",
    )
    assert extract_product_rating(product) == {"rating": 4.2, "rating_count": 10}


def test_normalizes_top_of_alternate_scale_to_5():
    # value at the very top of a 0-10 scale must land on exactly 5, not
    # overflow/overfill a 5-star row.
    product = _product(
        rating='{"scale_min":"0","scale_max":"10","value":"10"}',
        rating_count="3",
    )
    assert extract_product_rating(product) == {"rating": 5.0, "rating_count": 3}


def test_missing_scale_fields_default_to_1_to_5():
    # Malformed/partial JSON missing scale_min/scale_max -- falls back to
    # the standard 1-5 assumption rather than raising.
    product = _product(rating='{"value":"3.5"}', rating_count="4")
    assert extract_product_rating(product) == {"rating": 3.5, "rating_count": 4}


def test_degenerate_scale_falls_back_to_raw_value_not_zerodiv():
    # scale_max == scale_min would divide by zero -- must not raise.
    product = _product(
        rating='{"scale_min":"5","scale_max":"5","value":"5"}',
        rating_count="1",
    )
    result = extract_product_rating(product)
    assert result is not None
    assert result["rating"] == 5.0


# ---- Lenient rating_count parsing ----

def test_rating_count_with_thousands_separator():
    product = _product(
        rating='{"scale_min":"1.0","scale_max":"5.0","value":"4.5"}',
        rating_count="1,234",
    )
    assert extract_product_rating(product) == {"rating": 4.5, "rating_count": 1234}


def test_rating_count_as_decimal_typed_string():
    # Shopify metafield typed as a decimal rather than an integer.
    product = _product(
        rating='{"scale_min":"1.0","scale_max":"5.0","value":"4.5"}',
        rating_count="128.0",
    )
    assert extract_product_rating(product) == {"rating": 4.5, "rating_count": 128}


def test_rating_count_completely_unparseable_fails_open():
    product = _product(
        rating='{"scale_min":"1.0","scale_max":"5.0","value":"4.5"}',
        rating_count="not-a-number",
    )
    assert extract_product_rating(product) is None
