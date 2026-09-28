"""Regression tests for the delivery-timeline phone → order lookup path.

Covers the production incident (WhatsApp, 2026-08-08 12:08 IST) where a
customer with a live order asked "How much time will it take?" and was told
"I couldn't find an active order associated with your phone number".

Two independent defects lined up:

1. ``_get_delivery_tools`` never bound ``get_recent_orders`` — the only tool
   that resolves orders from a phone number. The model called it anyway
   (``get_order_details``' docstring tells it to) and got back
   "Error: get_recent_orders is not a valid tool".
2. It then fell back to ``get_order_details(order_id="<the customer's phone>")``,
   which happily expanded the phone into "#gv919310228406" order-name lookups
   and 404-ed. The model read that 404 as "this customer has no order".

These tests run fully offline — no Shopify, no LLM, no DB.
"""

import pytest

from fashion_bot.core.tool_registry import _get_delivery_tools
from fashion_bot.tool_factory import _looks_like_customer_phone

CUSTOMER_PHONE = "919310228406"  # as it arrives from the Gupshup webhook
STATE = {"phone_number": CUSTOMER_PHONE, "client_id": "test-client"}


def _tool_names(tools):
    return {getattr(t, "name", None) for t in tools}


def test_delivery_agent_binds_get_recent_orders():
    """The delivery agent must have a phone → orders path.

    Without this the node can only look up an order the customer names
    explicitly, which is the minority of delivery-timing questions.
    """
    tools = _get_delivery_tools(STATE, messages_list=[], client_id="test-client")
    names = _tool_names(tools)
    assert "get_recent_orders" in names
    assert "get_order_details" in names


def test_delivery_agent_keeps_its_existing_tools():
    """Adding the order lookup must not displace the delivery toolset."""
    tools = _get_delivery_tools(STATE, messages_list=[], client_id="test-client")
    names = _tool_names(tools)
    for expected in (
        "get_delivery_estimate_tool_enhanced",
        "check_product_availability_tool",
        "is_url_in_valid_domain_tool",
        "search_products",
        "find_product_by_id",
    ):
        assert expected in names, f"{expected} missing from delivery toolset"


@pytest.mark.parametrize(
    "order_id",
    [
        CUSTOMER_PHONE,          # exactly what the model passed in production
        "9310228406",            # 10-digit form
        "+91 93102 28406",       # formatted
    ],
)
def test_phone_passed_as_order_id_is_rejected(order_id):
    assert _looks_like_customer_phone(order_id, STATE) is True


@pytest.mark.parametrize(
    "order_id",
    [
        "gv17598",               # the customer's real order name
        "#gv17598",
        "17598",
        "7108744020290",         # Shopify numeric order id — 13 digits, not the phone
        "",
    ],
)
def test_real_order_ids_are_not_rejected(order_id):
    assert _looks_like_customer_phone(order_id, STATE) is False


def test_other_phone_is_not_rejected():
    """Only the *known* customer phone is refused.

    A long numeric order name that happens not to match any phone on file must
    still reach Shopify rather than being silently dropped.
    """
    assert _looks_like_customer_phone("9999999999", STATE) is False


def test_explicit_phone_argument_is_also_matched():
    """The guard checks the tool's phone_number arg, not just state."""
    assert (
        _looks_like_customer_phone("8160594535", {}, phone_number="+918160594535")
        is True
    )
