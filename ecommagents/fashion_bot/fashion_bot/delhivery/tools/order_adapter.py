"""
Delhivery OrderInterface adapter — thin shim that delegates to
DelhiveryLogisticsAdapter. Mirrors ShiprocketOrderAdapter so the orchestrator
can address Delhivery via the same vendor=... factory call.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fashion_bot.interfaces.order import OrderInterface
from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)


class DelhiveryOrderAdapter(OrderInterface):
    """OrderInterface adapter for Delhivery (delegates to logistics adapter)."""

    def __init__(self, client_id: Optional[str] = None):
        self.client_id = client_id

    def _resolve_client_id(self, state: Optional[Dict] = None) -> Optional[str]:
        if self.client_id:
            return self.client_id
        if state:
            return state.get("client_id")
        return None

    @staticmethod
    def _order_to_mapping(order: Any) -> Dict[str, Any]:
        if isinstance(order, dict) and "orders" in order:
            nested = order.get("orders") or []
            return DelhiveryOrderAdapter._order_to_mapping(nested[0]) if nested else {}
        if isinstance(order, dict):
            base = dict(order.get("order_data") or order)
            if order.get("order_data"):
                base.setdefault("order_id", order.get("order_id"))
                base.setdefault("channel_order_id", order.get("channel_order_id"))
                base.setdefault("partner_status", order.get("status"))
                base.setdefault("shipment_status", order.get("shipment_status"))
            return base
        return {}

    # ── interface methods ─────────────────────────────────────────────

    def get_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        raise RuntimeError(
            "DelhiveryOrderAdapter.get_order_details is async-only. "
            "Use `await aget_order_details(...)`."
        )

    async def aget_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"DelhiveryAdapter: getting order details for {order_id}")
        try:
            from fashion_bot.delhivery.tools.logistics_adapter import (
                DelhiveryLogisticsAdapter,
            )

            logistics = DelhiveryLogisticsAdapter(client_id=self._resolve_client_id(state))
            orders = await logistics.aget_matching_orders(order_id, state=state)
            return {"orders": orders}
        except Exception as exc:
            log_with_trace_id(state, f"Delhivery lookup failed: {exc}", "error")
            return {"orders": []}

    def get_orders_by_customer_phone(self, phone: str, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        raise RuntimeError(
            "DelhiveryOrderAdapter.get_orders_by_customer_phone is async-only."
        )

    async def aget_orders_by_customer_phone(
        self,
        phone: str,
        limit: int = 5,
        state: Optional[Dict] = None,
    ) -> List[Any]:
        # Delhivery does not expose a "find by phone" API. The orchestrator
        # already discovers orders via Shopify; this is a no-op shim.
        log_with_trace_id(
            state,
            "DelhiveryAdapter: phone lookup is unsupported on Delhivery; returning [].",
        )
        return []

    def create_order(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        raise NotImplementedError()

    def cancel_order(
        self,
        order_id: str,
        reason: str = "",
        state: Optional[Dict] = None,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        raise NotImplementedError()

    async def acancel_order(
        self,
        order_id: str,
        reason: str = "",
        state: Optional[Dict] = None,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        try:
            from fashion_bot.delhivery.tools.logistics_adapter import (
                DelhiveryLogisticsAdapter,
            )

            logistics = DelhiveryLogisticsAdapter(client_id=self._resolve_client_id(state))
            result = await logistics.acancel_shipment(order_id, state=state)
            if result.get("success") and reason:
                result["cancellation_reason"] = reason
            return result
        except Exception as exc:
            log_with_trace_id(state, f"Delhivery cancellation failed: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def update_order(self, order_id: str, update_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        raise NotImplementedError()

    async def aupdate_order(
        self,
        order_id: str,
        update_data: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        try:
            from fashion_bot.delhivery.tools.logistics_adapter import (
                DelhiveryLogisticsAdapter,
            )

            logistics = DelhiveryLogisticsAdapter(client_id=self._resolve_client_id(state))
            update_type = (update_data.get("update_type") or "").lower()

            if update_type == "phone" and update_data.get("new_phone"):
                return await logistics.aupdate_shipment_phone(
                    order_id, update_data["new_phone"], state=state,
                )
            if update_type == "email" and update_data.get("new_email"):
                return await logistics.aupdate_shipment_email(
                    order_id, update_data["new_email"], state=state,
                )
            if update_type == "name":
                return await logistics.aupdate_shipment_name(
                    order_id,
                    update_data.get("first_name", ""),
                    update_data.get("last_name", ""),
                    state=state,
                )

            address_payload: Dict[str, Any] = {}
            new_address = update_data.get("new_address")
            if new_address:
                parts = [p.strip() for p in str(new_address).split("|")]
                if len(parts) >= 6:
                    address_payload = {
                        "name": parts[0],
                        "address1": parts[1],
                        "address2": parts[2] if len(parts) > 2 else "",
                        "city": parts[3] if len(parts) > 3 else "",
                        "state": parts[4] if len(parts) > 4 else "",
                        "zip": parts[5] if len(parts) > 5 else "",
                    }
                    if len(parts) > 6 and parts[6]:
                        address_payload["phone"] = parts[6]
                else:
                    address_payload = {"address": str(new_address)}

            if update_data.get("new_phone"):
                address_payload["phone"] = update_data["new_phone"]

            if update_type in {"address", "multiple"} and address_payload:
                return await logistics.aupdate_shipment_address(
                    order_id, address_payload, state=state,
                )

            if update_type in {"instructions", "note", "notes"}:
                return {
                    "success": True,
                    "order_id": order_id,
                    "skipped": True,
                    "message": (
                        f"Delhivery does not support direct '{update_type}' updates."
                    ),
                }

            return {
                "success": False,
                "order_id": order_id,
                "error": (
                    f"Update type '{update_type or 'unknown'}' is not supported "
                    "for Delhivery orders"
                ),
            }
        except Exception as exc:
            log_with_trace_id(state, f"Delhivery update failed: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    async def aupdate_order_phone(
        self,
        order_id: str,
        new_phone: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.delhivery.tools.logistics_adapter import (
            DelhiveryLogisticsAdapter,
        )

        logistics = DelhiveryLogisticsAdapter(client_id=self._resolve_client_id(state))
        return await logistics.aupdate_shipment_phone(order_id, new_phone, state=state)

    async def aupdate_order_email(
        self,
        order_id: str,
        new_email: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.delhivery.tools.logistics_adapter import (
            DelhiveryLogisticsAdapter,
        )

        logistics = DelhiveryLogisticsAdapter(client_id=self._resolve_client_id(state))
        return await logistics.aupdate_shipment_email(order_id, new_email, state=state)

    async def aadd_order_note(
        self,
        order_id: str,
        note: str,
        state: Optional[Dict] = None,
        order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(
            state,
            f"DelhiveryAdapter: skipping note add for order {order_id} (no notes API)",
        )
        return {
            "success": True,
            "order_id": order_id,
            "note_added": note,
            "message": "Delhivery does not support order notes; note skipped",
        }

    async def aadd_order_tags(
        self,
        order_id: str,
        tags: List[str],
        state: Optional[Dict] = None,
        order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(
            state,
            f"DelhiveryAdapter: skipping tag add for order {order_id} (no tags API)",
        )
        return {
            "success": True,
            "order_id": order_id,
            "tags_added": tags,
            "message": "Delhivery does not support order tags; tags skipped",
        }

    def filter_delivered_orders(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        delivered = {"delivered", "rto delivered"}
        return [
            order for order in orders
            if (self._order_to_mapping(order).get("partner_status") or "").lower() in delivered
        ]

    def format_delivered_orders_for_display(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        formatted = []
        for order in orders:
            data = self._order_to_mapping(order)
            shipments = data.get("shipments", {}) or {}
            formatted.append({
                "order_id": data.get("order_id"),
                "channel_order_id": data.get("channel_order_id"),
                "awb_code": shipments.get("awb"),
                "status": data.get("partner_status"),
                "delivered_date": shipments.get("delivered_date") or data.get("updated_at"),
                "created_at": data.get("created_at"),
                "items": [
                    {
                        "name": p.get("name"),
                        "quantity": p.get("quantity", 1),
                        "sku": p.get("sku"),
                    }
                    for p in (data.get("products") or data.get("items") or [])
                ],
            })
        return formatted

    def build_delivered_orders_response(
        self,
        orders: List[Dict[str, Any]],
        phone_number: str,
    ) -> Dict[str, Any]:
        delivered = self.filter_delivered_orders(orders)
        formatted = self.format_delivered_orders_for_display(delivered)
        return {
            "success": True,
            "phone_number": phone_number,
            "total_orders": len(orders),
            "delivered_orders_count": len(delivered),
            "delivered_orders": formatted,
            "message": (
                f"Found {len(delivered)} delivered order(s) eligible for return/exchange."
                if delivered else "No delivered orders found for this customer."
            ),
        }
