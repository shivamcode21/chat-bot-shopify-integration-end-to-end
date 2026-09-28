"""A Shopify cancel that the courier does not confirm must escalate.

The dispatched-integrated branch treats Shopify's cancel as success on its own,
so a partner that refuses or errors used to be OR-ed away: the customer was told
the order was cancelled and refunded while the shipment kept moving. These tests
pin the escalation, its ordering against the refund, and the fact that a
confirmed cancel stays silent.

Partner-agnostic on purpose -- the hole is in shared cancel logic and reaches
every integrated partner, not just the split ClickPost orders that made it
reachable.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fashion_bot.core.orchestrator import CancellationOrchestrator


ORDER_DTO = {
    "order_id": "V0018267",
    "financial_status": "paid",
    "tracking_company": "Shadowfax",
    "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=SF3783013483VAO",
    "fulfillment_status": "fulfilled",
}


def _order_service(calls):
    """Order service whose refund records its position in the call order."""
    svc = MagicMock()
    svc.aget_order_details = AsyncMock(return_value=ORDER_DTO)
    svc.acancel_order = AsyncMock(return_value={"success": True})
    svc.aadd_order_note = AsyncMock(return_value=True)

    async def _refund(*_a, **_kw):
        calls.append("refund")
        return {"success": True}

    svc.arefund_order = AsyncMock(side_effect=_refund)
    return svc


async def _run(logistics_cancel_result, calls):
    """Drive acancel_order down the dispatched-integrated branch."""
    async def _escalate(*_a, **kw):
        calls.append("escalate")
        return {"success": True, "category": kw.get("category")}

    with patch("fashion_bot.core.orchestrator.ServiceFactory.aget_order_service",
               AsyncMock(return_value=_order_service(calls))), \
         patch("fashion_bot.core.orchestrator.OrderStatusOrchestrator._is_order_cancelled_or_voided",
               MagicMock(return_value=False)), \
         patch("fashion_bot.core.orchestrator.OrderStatusOrchestrator._is_order_new",
               MagicMock(return_value=False)), \
         patch("fashion_bot.utils.delivery_partner_utils.aresolve_effective_partner_for_order",
               AsyncMock(return_value=("clickpost", True))), \
         patch("fashion_bot.core.logistics_router.LogisticsRouter.acancel_first_success",
               AsyncMock(return_value=logistics_cancel_result)), \
         patch("fashion_bot.core.orchestrator.EscalationOrchestrator.aescalate_to_agent",
               AsyncMock(side_effect=_escalate)):
        return await CancellationOrchestrator.acancel_order(
            "V0018267", "customer changed mind", state={"client_id": "test-client"},
        )


@pytest.mark.asyncio
async def test_partner_refusal_escalates_before_the_refund():
    """The multi-parcel refusal reaches ops, and it does so while the money is
    still held -- escalating after the refund leaves nothing to act on."""
    calls = []
    result = await _run(
        {"success": False, "status": "cancel_multi_parcel_unsupported",
         "requires_manual_action": True},
        calls,
    )

    # Pins the branch under test: if the routing conditions change, this fails
    # rather than passing vacuously on a path that never escalates.
    assert result["routing"] == "dispatched_integrated"
    assert result["shopify_cancel"] == {"success": True}
    assert result["escalation"], "a refused partner cancel must escalate"
    assert result["escalation"]["category"] == (
        "Order Cancellation - Partner Cancel Not Confirmed"
    )
    assert calls == ["escalate", "refund"], (
        f"escalation must precede the refund, got {calls}"
    )


@pytest.mark.asyncio
async def test_partner_error_escalates_too():
    """An outage is the same situation as a refusal: the shipment is still
    moving and nobody has been told."""
    calls = []
    result = await _run({"success": False, "error": "connection reset"}, calls)

    assert result["escalation"], "a failed partner cancel must escalate"


@pytest.mark.asyncio
async def test_confirmed_cancel_does_not_escalate():
    """The normal path stays quiet -- otherwise every cancellation would page
    someone."""
    calls = []
    result = await _run({"success": True}, calls)

    assert not result.get("escalation"), (
        "a confirmed partner cancel must not raise manual work"
    )
    assert "refund" in calls
