"""
ClickPost OrderInterface adapter -- shipment lookup only.

ClickPost is a shipping aggregator: it tracks and cancels shipments but does
not own orders, which stay in Shopify. The order-management methods here
(create, cancel, update, customer lookup) therefore return a "not supported"
response by design rather than pending work, and perform no I/O.

``aget_order_details`` is the exception and does issue live calls. The
order-status race asks every connected partner for its view of an order, and
returning nothing there would leave each ClickPost-fulfilled order falling
back to a Shopify-derived status. It delegates to the logistics adapter,
which resolves the waybill from the Shopify fulfillment before querying
ClickPost.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fashion_bot.interfaces.order import OrderInterface
from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)

_NOT_SUPPORTED = (
    "ClickPost is a shipping aggregator and does not manage orders; "
    "order operations belong to Shopify."
)


class ClickPostOrderAdapter(OrderInterface):
    """Placeholder ``OrderInterface`` adapter for ClickPost. Async-only, no I/O."""

    def __init__(self, client_id: Optional[str] = None):
        self.client_id = client_id

    @staticmethod
    def _raise_sync_unavailable(method_name: str):
        raise RuntimeError(
            f"ClickPostOrderAdapter.{method_name} is async-only. "
            f"Use the corresponding `await a...` method."
        )

    # ── abstract sync methods ───────────────────────────────────────────

    def get_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_order_details")

    def get_orders_by_customer_phone(
        self, phone: str, limit: int = 5, state: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        self._raise_sync_unavailable("get_orders_by_customer_phone")

    def create_order(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("create_order")

    def cancel_order(
        self,
        order_id: str,
        reason: str = "",
        state: Optional[Dict] = None,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        self._raise_sync_unavailable("cancel_order")

    def update_order(self, order_id: str, update_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("update_order")

    def filter_delivered_orders(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # No I/O, nothing to filter yet -- ClickPost has no order surface.
        return []

    def format_delivered_orders_for_display(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return []

    def build_delivered_orders_response(self, orders: List[Dict[str, Any]], phone_number: str) -> Dict[str, Any]:
        return {
            "success": True,
            "phone_number": phone_number,
            "total_orders": 0,
            "delivered_orders_count": 0,
            "delivered_orders": [],
            "message": _NOT_SUPPORTED,
        }

    # ── async variants ──────────────────────────────────────────────────

    async def aget_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Return the shipment in the ``{"orders": [...]}`` wrapper the
        order-status race expects.

        ClickPost has no order surface of its own -- the shipment is looked up
        through the logistics adapter, which resolves the waybill from the
        Shopify fulfillment first. Returning an empty list here would leave
        every fulfilled order falling back to the Shopify-derived status.
        """
        log_with_trace_id(state, f"ClickPostAdapter: getting order details for {order_id}")
        try:
            from fashion_bot.clickpost.tools.logistics_adapter import (
                ClickPostLogisticsAdapter,
            )

            client_id = self.client_id or (state.get("client_id") if state else None)
            logistics = ClickPostLogisticsAdapter(client_id=client_id)
            orders = await logistics.aget_matching_orders(order_id, state=state)
            return {"orders": orders}
        except Exception as exc:
            log_with_trace_id(state, f"ClickPost order lookup failed: {exc}", "error")
            # Fail open, but say why: the race reads a bare empty list as a
            # clean "not my order" and would file an outage under
            # shopify_fallback_logistics_miss instead of ..._error.
            return {"orders": [], "error": str(exc)}

    async def aget_orders_by_customer_phone(
        self,
        phone: str,
        limit: int = 5,
        state: Optional[Dict] = None,
    ) -> List[Any]:
        return []

    async def acreate_order(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        return {"success": False, "message": _NOT_SUPPORTED}

    async def acancel_order(
        self,
        order_id: str,
        reason: str = "",
        state: Optional[Dict] = None,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        return {"success": False, "order_id": order_id, "message": _NOT_SUPPORTED}

    async def aupdate_order(
        self,
        order_id: str,
        update_data: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        return {"success": False, "order_id": order_id, "message": _NOT_SUPPORTED}
