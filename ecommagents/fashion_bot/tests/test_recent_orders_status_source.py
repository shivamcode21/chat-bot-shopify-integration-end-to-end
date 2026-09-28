"""Order status in ``get_recent_orders`` comes from the carrier, not from tags.

The summary loop used to force ``status = "RTO"`` whenever the Shopify tag
blob contained the substring "rto". Shiprocket Fastrr stamps
``rto_prediction_high`` on risky-looking orders at checkout -- an RTO *risk
score*, assigned before the parcel ships -- so orders that were moving
normally were reported to customers as returned to the seller.

Production case: order gv18287 (Concept Groove, 2026-08-24). Shopify had it
FULFILLED / IN_TRANSIT on Blue Dart AWB 77928882776 with ETD 28 Aug and
Shiprocket had it PICKUP SCHEDULED; neither system said RTO. The agent told
the customer it was "marked as RTO (Returned to Origin)" for two days.

Status now routes through ``classify_status`` -- the classifier
``get_order_details`` already uses -- so a real RTO still reports as RTO and
the two order tools agree on the same order.
"""
import pytest
from unittest.mock import AsyncMock, patch

from fashion_bot.tool_factory import cancel_or_update_tools_factory

# The live tag string on gv18287, read from the Shopify Admin API.
GV18287_TAGS = (
    "Beyond Knowing - L, Beyond Knowing - S, BLOOMERCE_UPDATED, fastrr, high, "
    "neccoinamount-0, neccoinamount-300, orig_PayU_txn_id:30265878938, "
    "orig_txn_id:8601398346050, PPCOD, rto_prediction_high, SIZE_CHANGE_CLONED, "
    "skip_rewarding_nector, SR_STANDARD, Standard, Tier1_city"
)


def _order(**overrides):
    order = {
        "name": "#gv18287",
        "order_number": "gv18287",
        "created_at": "2026-08-22T21:09:08+05:30",
        "financial_status": "partially_paid",
        "fulfillment_status": "fulfilled",
        "cancelled_at": None,
        "tags": GV18287_TAGS,
        "total_price": "1699.00",
        "currency": "INR",
        "line_items": [],
        "fulfillments": [
            {
                "tracking_numbers": ["77928882776"],
                "tracking_url": "https://groovee.shiprocket.co/tracking/77928882776",
                "tracking_company": "Blue Dart Surface",
                "shipment_status": "in_transit",
            }
        ],
    }
    order.update(overrides)
    return order


async def _recent_orders(order):
    state = {
        "client_id": "test-client",
        "phone_number": "+919354333154",
        "messages": [],
    }
    with patch(
        "fashion_bot.core.orchestrator.UtilityOrchestrator.aget_recent_actionable_orders",
        AsyncMock(return_value={"success": True, "orders": [order]}),
    ), patch(
        "fashion_bot.core.orchestrator.UtilityOrchestrator.aget_recent_orders_all_statuses",
        AsyncMock(return_value={"success": True, "orders": [order]}),
    ):
        built = cancel_or_update_tools_factory(state, state["messages"])
        if hasattr(built, "__await__"):
            built = await built
        tools = {t.name: t for t in built}
        result = await tools["get_recent_orders"].ainvoke(
            {"phone_number": "+919354333154"}
        )
    return (result.get("orders") or [{}])[0]


@pytest.mark.asyncio
async def test_rto_prediction_tag_does_not_become_rto_status():
    """The gv18287 regression: a risk-score tag is not a shipment state."""
    order = await _recent_orders(_order())

    assert order["status"] != "RTO"
    assert order["status"] == "In Transit"


@pytest.mark.asyncio
async def test_unrelated_tag_containing_rto_substring_is_ignored():
    """The old check was a bare substring, so ordinary words tripped it."""
    order = await _recent_orders(_order(tags="assorted, carton-pack, Porto"))

    assert order["status"] == "In Transit"


@pytest.mark.asyncio
async def test_real_rto_from_partner_status_still_reports_rto():
    """Removing the tag branch must not lose genuine returns."""
    order = await _recent_orders(
        _order(partner_status="RTO DELIVERED", fulfillments=[])
    )

    assert order["status"] == "RTO"


@pytest.mark.asyncio
async def test_carrier_delivered_beats_tags():
    order = await _recent_orders(
        _order(
            fulfillments=[
                {
                    "tracking_numbers": ["77928882776"],
                    "tracking_url": "https://groovee.shiprocket.co/tracking/77928882776",
                    "tracking_company": "Blue Dart Surface",
                    "shipment_status": "delivered",
                }
            ]
        )
    )

    assert order["status"] == "Delivered"


@pytest.mark.asyncio
async def test_cancelled_order_reports_cancelled():
    """gv18286 / gv18288 were genuinely cancelled -- that must survive."""
    order = await _recent_orders(
        _order(cancelled_at="2026-08-22T15:39:06Z", fulfillments=[])
    )

    assert order["status"] == "Cancelled"


@pytest.mark.asyncio
async def test_fulfilled_without_carrier_report_keeps_shopify_state():
    """classify_status has nothing to echo here; don't emit a blank status."""
    order = await _recent_orders(
        _order(
            fulfillments=[
                {
                    "tracking_numbers": ["77928882776"],
                    "tracking_url": "https://groovee.shiprocket.co/tracking/77928882776",
                    "tracking_company": "Blue Dart Surface",
                    "shipment_status": "",
                }
            ]
        )
    )

    assert order["status"] == "fulfilled"


@pytest.mark.asyncio
async def test_unfulfilled_order_reports_not_yet_dispatched():
    order = await _recent_orders(_order(fulfillment_status=None, fulfillments=[]))

    assert order["status"] == "Not Yet Dispatched"
