"""Split orders in ``get_recent_orders``.

An order can ship as several parcels, each with its own tracking link that
follows only that parcel. The summary loop stops at the first fulfillment it
can read a link from, so the ``parcels`` list is what tells the agent the
other boxes exist.

Vendor-neutral on purpose: this reads Shopify's fulfillment records, so every
tenant's payload gains the key, not only the partners with an integration.
The fixtures below use a Delhivery order for that reason.
"""
import pytest
from unittest.mock import AsyncMock, patch

from fashion_bot.tool_factory import cancel_or_update_tools_factory


def _fulfillment(awb="", url="", company="Delhivery", status="in_transit"):
    return {
        "tracking_numbers": [awb] if awb else [],
        "tracking_url": url,
        "tracking_company": company,
        "shipment_status": status,
    }


def _order(fulfillments):
    return {
        "name": "#V0018267",
        "order_number": "V0018267",
        "created_at": "2026-08-10T10:00:00+05:30",
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "total_price": "7656.80",
        "currency": "INR",
        "line_items": [],
        "fulfillments": fulfillments,
    }


async def _recent_orders(fulfillments):
    """Invoke get_recent_orders over one order with the given fulfillments."""
    state = {
        "client_id": "test-client",
        "phone_number": "+918555952389",
        "messages": [],
    }
    with patch(
        "fashion_bot.core.orchestrator.UtilityOrchestrator.aget_recent_actionable_orders",
        AsyncMock(return_value={"success": True, "orders": [_order(fulfillments)]}),
    ), patch(
        "fashion_bot.core.orchestrator.UtilityOrchestrator.aget_recent_orders_all_statuses",
        AsyncMock(return_value={"success": True, "orders": [_order(fulfillments)]}),
    ):
        built = cancel_or_update_tools_factory(state, state["messages"])
        if hasattr(built, "__await__"):
            built = await built
        tools = {t.name: t for t in built}
        result = await tools["get_recent_orders"].ainvoke({"phone_number": "+918555952389"})
    return (result.get("orders") or [{}])[0]


@pytest.mark.asyncio
async def test_single_parcel_order_has_no_parcels_key():
    """One shipment is fully described by the flat fields; a one-entry list
    would invite the agent to talk about parcels that don't exist."""
    order = await _recent_orders([
        _fulfillment(awb="AWB1", url="https://track.example/AWB1"),
    ])

    assert "parcels" not in order
    assert order["awb"] == "AWB1"


@pytest.mark.asyncio
async def test_split_order_lists_every_parcel():
    """Two fulfillments, two waybills, two links -> both reach the agent."""
    order = await _recent_orders([
        _fulfillment(awb="AWB1", url="https://track.example/AWB1"),
        _fulfillment(awb="AWB2", url="https://track.example/AWB2"),
    ])

    assert [p["awb"] for p in order["parcels"]] == ["AWB1", "AWB2"]
    assert [p["tracking_url"] for p in order["parcels"]] == [
        "https://track.example/AWB1", "https://track.example/AWB2",
    ]
    # Same key name the partner-backed path uses, so one prompt rule covers
    # whichever tool the agent called.
    assert all("status" in p for p in order["parcels"])


@pytest.mark.asyncio
async def test_same_waybill_on_two_fulfillments_collapses():
    """Shopify can record one shipment twice; that is one parcel, not two."""
    order = await _recent_orders([
        _fulfillment(awb="AWB1", url="https://track.example/AWB1"),
        _fulfillment(awb="AWB1", url="https://track.example/AWB1"),
    ])

    assert "parcels" not in order, "one shipment recorded twice is still one parcel"


@pytest.mark.asyncio
async def test_url_only_parcels_are_not_merged():
    """Two fulfillments with links but no waybill are distinct parcels -- the
    empty-AWB case the (awb, tracking_url) key exists to handle. Deduping on
    the AWB alone would collapse them into one and hide a box."""
    order = await _recent_orders([
        _fulfillment(awb="", url="https://track.example/one"),
        _fulfillment(awb="", url="https://track.example/two"),
    ])

    assert [p["tracking_url"] for p in order["parcels"]] == [
        "https://track.example/one", "https://track.example/two",
    ]
