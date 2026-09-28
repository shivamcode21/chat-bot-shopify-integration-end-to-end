"""Unit tests for abandoned-checkout phone extraction + cart-topic gating.

Covers the production gap where the recovery feature sent zero WhatsApp
messages because:
  1. `carts/create` / `carts/update` events (no contact fields) were processed
     as abandoned checkouts, producing only "guest_checkout_phone_not_available".
  2. Guest-checkout phones live in the top-level `phone` / `billing_address.phone`
     / `customer.default_address.phone` fields, which the primary extractor
     (`extract_phone_number`, customer.phone + shipping_address.phone) never read.

These tests exercise the small, additive helpers introduced to close that gap.
They run fully offline (pure functions, no Shopify/Gupshup calls).
"""

import pytest

from fashion_bot.shopify.webhook.abandoned_checkout_webhook import (
    extract_phone_number,
    extract_phone_fallback,
    is_cart_topic,
    _normalize_phone,
)


# --- _normalize_phone -------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  +919812345678 ", "919812345678"),  # strips space + leading '+'
        ("919812345678", "919812345678"),
        ("", None),
        ("   ", None),
        (None, None),
        (123456, None),  # non-string degrades to None
    ],
)
def test_normalize_phone(raw, expected):
    assert _normalize_phone(raw) == expected


@pytest.mark.unit
def test_normalize_phone_is_idempotent():
    once = _normalize_phone("+919812345678")
    assert _normalize_phone(once) == once == "919812345678"


# --- is_cart_topic ----------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "topic, expected",
    [
        ("carts/update", True),
        ("carts/create", True),
        ("CARTS/CREATE", True),  # case-insensitive
        (" carts/update ", True),  # whitespace tolerant
        ("checkouts/update", False),
        ("checkouts/create", False),
        ("orders/create", False),
        (None, False),
        ("", False),
    ],
)
def test_is_cart_topic(topic, expected):
    assert is_cart_topic(topic) is expected


# --- extract_phone_fallback (additive) --------------------------------------

@pytest.mark.unit
def test_fallback_reads_top_level_phone_for_guest():
    """Guest checkout: customer null, phone only at top level."""
    wd = {"customer": None, "shipping_address": [], "billing_address": {}, "phone": "+919800000000"}
    assert extract_phone_number(wd) is None
    assert extract_phone_fallback(wd) == "919800000000"


@pytest.mark.unit
def test_fallback_reads_sms_marketing_phone():
    wd = {"customer": {}, "shipping_address": {}, "sms_marketing_phone": "919700000000"}
    assert extract_phone_number(wd) is None
    assert extract_phone_fallback(wd) == "919700000000"


@pytest.mark.unit
def test_fallback_reads_billing_address_phone():
    wd = {"customer": {}, "shipping_address": {}, "billing_address": {"phone": "9811111111"}}
    assert extract_phone_number(wd) is None
    assert extract_phone_fallback(wd) == "9811111111"


@pytest.mark.unit
def test_fallback_reads_customer_default_address_phone():
    wd = {"customer": {"default_address": {"phone": "9822222222"}}, "shipping_address": {}}
    assert extract_phone_number(wd) is None
    assert extract_phone_fallback(wd) == "9822222222"


@pytest.mark.unit
def test_fallback_returns_none_when_no_phone_anywhere():
    wd = {"customer": {}, "shipping_address": {}, "billing_address": {}}
    assert extract_phone_fallback(wd) is None


@pytest.mark.unit
def test_primary_path_still_wins_and_is_unchanged():
    """The existing extractor still handles customer.phone / shipping_address.phone."""
    assert extract_phone_number({"customer": {"phone": "+919900000000"}}) == "919900000000"
    assert extract_phone_number(
        {"customer": {}, "shipping_address": {"phone": "919900000001"}}
    ) == "919900000001"
