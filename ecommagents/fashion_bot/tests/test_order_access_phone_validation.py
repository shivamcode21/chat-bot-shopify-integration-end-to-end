"""Unit tests for order-access phone validation.

Covers the regression where `get_recent_orders` listed an order for a phone
but `_avalidate_phone_for_order_access` then refused it because the entered
phone wasn't on the order's *shipping/billing* fields — even though it was on
the embedded customer object (`customer.default_address.phone`).

These tests run fully offline: a cached base result (DTO) and a cached raw
record are passed, so the validator never resolves a service or hits Shopify.
"""

import pytest

from fashion_bot.tool_factory import _avalidate_phone_for_order_access

# Mirrors the real sereko case: both orders belong to one customer whose
# default-address phone is ENTERED_PHONE, while order #63084's own
# shipping/billing/order/customer phones are the account number.
ENTERED_PHONE = "8160594535"
ACCOUNT_PHONE = "8469153384"

# Raw Shopify order record for #63084: the entered phone appears ONLY in
# customer.default_address.phone (not in shipping/billing/order/customer.phone).
RAW_63084 = {
    "name": "#63084",
    "phone": f"+91{ACCOUNT_PHONE}",
    "shipping_address": {"phone": ACCOUNT_PHONE},
    "billing_address": {"phone": ACCOUNT_PHONE},
    "customer": {
        "id": 10572903874873,
        "phone": f"+91{ACCOUNT_PHONE}",
        "default_address": {"phone": ENTERED_PHONE},
    },
}

# Processed DTO as get_order_details builds it — no usable phone (mirrors the
# logged "No valid phone in order DTO" condition that triggered the bug).
DTO_63084 = {"name": "#63084", "customer_phone": "", "billing_phone": ""}
BASE_63084 = {"total_orders_found": 1, "orders": [DTO_63084]}


async def _validate(entered, base, raw):
    return await _avalidate_phone_for_order_access(
        "63084",
        {"phone_number": entered, "client_id": "test-client"},
        phone_number=entered,
        cached_base_result=base,
        prefetched_raw_record=raw,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_entered_phone_on_customer_default_address_is_allowed():
    """The exact failing case: phone only on customer.default_address.phone."""
    res = await _validate(ENTERED_PHONE, BASE_63084, RAW_63084)
    assert res["should_block"] is False
    assert res["valid"] is True
    # order_data must be preserved for downstream update-rule checks.
    assert res["order_data"] is BASE_63084


@pytest.mark.unit
@pytest.mark.asyncio
async def test_account_phone_is_allowed():
    res = await _validate(ACCOUNT_PHONE, BASE_63084, RAW_63084)
    assert res["should_block"] is False
    assert res["valid"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unrelated_phone_is_blocked():
    res = await _validate("9999999999", BASE_63084, RAW_63084)
    assert res["should_block"] is True
    assert res["valid"] is False
    assert res["order_data"] is BASE_63084


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_with_no_phones_is_blocked():
    raw = {"name": "#0", "customer": {}, "shipping_address": {}, "billing_address": {}}
    base = {"orders": [{"name": "#0", "customer_phone": "", "billing_phone": ""}]}
    res = await _avalidate_phone_for_order_access(
        "0",
        {"phone_number": ENTERED_PHONE},
        phone_number=ENTERED_PHONE,
        cached_base_result=base,
        prefetched_raw_record=raw,
    )
    assert res["should_block"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_match_on_dto_phone_without_raw_record():
    """When the entered phone is already on the DTO, no raw record is needed."""
    base = {"orders": [{"name": "#9", "customer_phone": ENTERED_PHONE, "billing_phone": ""}]}
    res = await _avalidate_phone_for_order_access(
        "9",
        {"phone_number": ENTERED_PHONE},
        phone_number=ENTERED_PHONE,
        cached_base_result=base,
        prefetched_raw_record=None,
    )
    assert res["should_block"] is False
    assert res["valid"] is True
