"""Regression tests for ``resolve_variant_match``.

Covers the production incident where an order size update failed with
"Could not find variant 'S' for this product": the product's Shopify variant
titles were compound ``"<Colour> / <Size>"`` strings, so a bare size like
``'S'`` never equalled the full title. The shared matcher must resolve size+
colour, size-only and colour-only products, and preserve the untouched option
on a single-attribute change.
"""

import pytest

from fashion_bot.utils.product_utils import resolve_variant_match


def _variant(vid, title, options, *, available=True, price="100"):
    return {
        "id": f"gid://shopify/ProductVariant/{vid}",
        "title": title,
        "price": price,
        "is_available": available,
        "selectedOptions": [{"name": name, "value": value} for name, value in options],
    }


def _matched_id(variant):
    return None if variant is None else variant["id"].split("/")[-1]


# ── Colour + Size compound titles (the Enamor incident) ──────────────────────

ENAMOR = [
    _variant("1", "Fog Green Soul Balance Graphic / S",
             [("Color", "Fog Green Soul Balance Graphic"), ("Size", "S")]),
    _variant("2", "Fog Green Soul Balance Graphic / M",
             [("Color", "Fog Green Soul Balance Graphic"), ("Size", "M")]),
    _variant("3", "Fog Green Soul Balance Graphic / L",
             [("Color", "Fog Green Soul Balance Graphic"), ("Size", "L")]),
]


def test_bare_size_anchored_on_current_variant():
    # The exact production failure: agent passes 'S', current line item is M.
    match = resolve_variant_match(ENAMOR, "S", current_variant_id="2", current_value="M")
    assert _matched_id(match) == "1"


def test_full_compound_title_matches_directly():
    match = resolve_variant_match(
        ENAMOR, "Fog Green Soul Balance Graphic / S",
        current_variant_id="2", current_value="M",
    )
    assert _matched_id(match) == "1"


def test_bare_size_without_anchor_falls_back_to_option_value():
    assert _matched_id(resolve_variant_match(ENAMOR, "L")) == "3"


# ── Multi-colour + Size: single-attribute change must keep the other option ──

MULTI = [
    _variant("10", "Red / S", [("Color", "Red"), ("Size", "S")]),
    _variant("11", "Red / M", [("Color", "Red"), ("Size", "M")]),
    _variant("12", "Blue / S", [("Color", "Blue"), ("Size", "S")]),
    _variant("13", "Blue / M", [("Color", "Blue"), ("Size", "M")]),
]


def test_size_change_preserves_colour():
    # Current Blue/M -> size S must resolve to Blue/S, not Red/S.
    match = resolve_variant_match(MULTI, "S", current_variant_id="13", current_value="Blue / M")
    assert _matched_id(match) == "12"


def test_colour_change_preserves_size():
    match = resolve_variant_match(MULTI, "Blue", current_variant_id="11", current_value="Red / M")
    assert _matched_id(match) == "13"


def test_full_compound_is_option_order_insensitive():
    assert _matched_id(resolve_variant_match(MULTI, "S / Blue")) == "12"


# ── Single-dimension products ────────────────────────────────────────────────

SIZE_ONLY = [
    _variant("20", "S", [("Size", "S")]),
    _variant("21", "M", [("Size", "M")]),
    _variant("22", "L", [("Size", "L")]),
]

COLOUR_ONLY = [
    _variant("30", "Red", [("Color", "Red")]),
    _variant("31", "Blue", [("Color", "Blue")]),
]


def test_size_only_product():
    assert _matched_id(resolve_variant_match(SIZE_ONLY, "M", current_variant_id="20", current_value="S")) == "21"


def test_size_only_casefold():
    assert _matched_id(resolve_variant_match(SIZE_ONLY, "l")) == "22"


def test_no_fuzzy_size_collapse():
    # Deliberate: matching is exact/casefold, not fuzzy. Requesting a size the
    # product does not carry must NOT silently collapse onto a near-neighbour
    # (e.g. 'XXL' must never resolve to an 'XL' variant).
    xl_only = [_variant("40", "XL", [("Size", "XL")]), _variant("41", "L", [("Size", "L")])]
    assert resolve_variant_match(xl_only, "XXL") is None


def test_colour_only_product():
    assert _matched_id(resolve_variant_match(COLOUR_ONLY, "Blue", current_variant_id="30", current_value="Red")) == "31"


# ── Non-matches and guards ───────────────────────────────────────────────────

def test_unknown_value_returns_none():
    assert resolve_variant_match(ENAMOR, "XXL", current_variant_id="2", current_value="M") is None


@pytest.mark.parametrize("variants,requested", [([], "S"), (ENAMOR, ""), (ENAMOR, "   ")])
def test_empty_inputs_return_none(variants, requested):
    assert resolve_variant_match(variants, requested) is None


# ── Irregular whitespace in store option values ──────────────────────────────
# Production incident (25 Aug 2026, order for 7395918227): the Shopify option
# values carried a stray double space ("Pack of 2  16g") while the agent passed
# the same label single-spaced, so every matching tier missed and the customer
# was told the order failed "due to a technical issue". Internal whitespace is
# never a meaningful part of an option value.

PACK = [
    _variant("50", "Pack of 1  8g", [("Size", "Pack of 1  8g")]),
    _variant("51", "Pack of 2  16g", [("Size", "Pack of 2  16g")]),
]


@pytest.mark.parametrize("requested", [
    "Pack of 2 16g",      # agent collapsed the store's double space
    "Pack of 2  16g",     # verbatim store value
    "pack of 2   16g",    # extra spacing, different case
    " Pack of 2 16g ",    # leading/trailing padding
])
def test_whitespace_variance_still_matches(requested):
    assert _matched_id(resolve_variant_match(PACK, requested)) == "51"


def test_whitespace_normalization_does_not_merge_distinct_variants():
    # Collapsing whitespace must not make two genuinely different packs equal.
    assert _matched_id(resolve_variant_match(PACK, "Pack of 1 8g")) == "50"


def test_whitespace_variance_in_compound_title():
    compound = [
        _variant("60", "Blue / Pack of 1  8g", [("Color", "Blue"), ("Size", "Pack of 1  8g")]),
        _variant("61", "Blue / Pack of 2  16g", [("Color", "Blue"), ("Size", "Pack of 2  16g")]),
    ]
    assert _matched_id(resolve_variant_match(compound, "Blue / Pack of 2 16g")) == "61"


def test_whitespace_variance_on_anchored_change():
    assert _matched_id(resolve_variant_match(
        PACK, "Pack of 2 16g", current_variant_id="50", current_value="Pack of 1  8g",
    )) == "51"


def test_whitespace_fix_does_not_weaken_exactness():
    # The whitespace tolerance must not open the door to fuzzy size collapsing.
    assert resolve_variant_match(PACK, "Pack of 3 24g") is None
