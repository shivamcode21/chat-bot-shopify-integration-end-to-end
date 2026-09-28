import logging
from typing import Dict, Any, Optional, List
from fashion_bot.interfaces.order import OrderInterface
from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)

class ShiprocketOrderAdapter(OrderInterface):
    def __init__(self, client_id: str = None):
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
            nested_orders = order.get("orders") or []
            if nested_orders:
                return ShiprocketOrderAdapter._order_to_mapping(nested_orders[0])
            return {}

        if isinstance(order, dict):
            base_data = dict(order.get("order_data") or order)
            if order.get("order_data"):
                base_data.setdefault("order_id", order.get("order_id"))
                base_data.setdefault("channel_order_id", order.get("channel_order_id"))
                base_data.setdefault("partner_status", order.get("status"))
                base_data.setdefault("shipment_status", order.get("shipment_status"))
            return base_data

        order_data = getattr(order, "order_data", None)
        if isinstance(order_data, dict):
            base_data = dict(order_data)
            base_data.setdefault("order_id", getattr(order, "order_id", ""))
            base_data.setdefault("channel_order_id", getattr(order, "channel_order_id", ""))
            base_data.setdefault("partner_status", getattr(order, "status", ""))
            base_data.setdefault("shipment_status", getattr(order, "shipment_status", ""))
            return base_data

        return {}

    @staticmethod
    def _normalize_phone_lookup_order(raw_order: Dict[str, Any]) -> Dict[str, Any]:
        order_data = dict(raw_order.get("order_data") or {})
        status = str(raw_order.get("status") or "").upper()
        phone = (
            order_data.get("billing_phone")
            or order_data.get("billing_customer_phone")
            or order_data.get("customer_phone")
            or order_data.get("shipping_phone")
            or ""
        )
        products = order_data.get("products") or order_data.get("items") or []

        line_items = [
            {
                "title": item.get("name") or item.get("product_name") or "Unknown item",
                "name": item.get("name") or item.get("product_name") or "Unknown item",
                "quantity": item.get("quantity") or item.get("units", 1),
                "sku": item.get("sku"),
            }
            for item in products
        ]

        normalized = dict(order_data)
        normalized.update(
            {
                "id": raw_order.get("order_id"),
                "order_id": raw_order.get("order_id"),
                "name": raw_order.get("channel_order_id") or raw_order.get("order_id"),
                "channel_order_id": raw_order.get("channel_order_id"),
                "partner_status": status,
                "shipment_status": raw_order.get("shipment_status") or status,
                "status": status,
                "customer_phone": phone,
                "billing_phone": phone,
                "line_items": line_items,
                "currency": order_data.get("currency") or "INR",
            }
        )

        if status == "CANCELED":
            normalized.setdefault("cancelled_at", order_data.get("updated_at") or order_data.get("created_at"))

        if status in {"DELIVERED", "RTO DELIVERED", "RETURN DELIVERED"}:
            normalized.setdefault("fulfillment_status", "fulfilled")
        elif status == "CANCELED":
            normalized.setdefault("fulfillment_status", "cancelled")
        else:
            normalized.setdefault("fulfillment_status", "unfulfilled")

        return normalized

    def get_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get order details from Shiprocket.
        Returns a Dict containing the list of orders found, to match Interface return type Dict.
        Structure: {"orders": [list of order objects]}
        """
        raise RuntimeError(
            "ShiprocketOrderAdapter.get_order_details is async-only. Use `await aget_order_details(...)`."
        )

    async def aget_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShiprocketAdapter: Async getting order status for {order_id}")
        try:
            from fashion_bot.shopify.modules.order_updation_api import aget_shiprocket_complete_order_data

            orders = await aget_shiprocket_complete_order_data(
                order_id,
                client_id=self._resolve_client_id(state),
            )
            return {"orders": orders}
        except Exception as exc:
            log_with_trace_id(state, f"Shiprocket lookup failed: {exc}", "error")
            return {"orders": []}

    def get_orders_by_customer_phone(self, phone: str, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        raise RuntimeError(
            "ShiprocketOrderAdapter.get_orders_by_customer_phone is async-only. Use `await aget_orders_by_customer_phone(...)`."
        )

    async def aget_orders_by_customer_phone(
        self,
        phone: str,
        limit: int = 5,
        state: Optional[Dict] = None,
    ) -> List[Any]:
        try:
            from fashion_bot.shiprocket.tools.logistics_adapter import ShiprocketLogisticsAdapter

            logistics_adapter = ShiprocketLogisticsAdapter(client_id=self._resolve_client_id(state))
            raw_orders = await logistics_adapter.aget_orders_by_phone(phone, state=state)
            orders = [self._normalize_phone_lookup_order(order) for order in raw_orders]
            orders.sort(
                key=lambda item: item.get("created_at", ""),
                reverse=True,
            )
            return orders[:limit]
        except Exception as exc:
            log_with_trace_id(state, f"Shiprocket phone lookup failed: {exc}", "error")
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
            from fashion_bot.shiprocket.tools.logistics_adapter import ShiprocketLogisticsAdapter

            logistics_adapter = ShiprocketLogisticsAdapter(client_id=self._resolve_client_id(state))
            result = await logistics_adapter.acancel_shipment(order_id, state=state)
            if result.get("success") and reason:
                result["cancellation_reason"] = reason
            return result
        except Exception as exc:
            log_with_trace_id(state, f"Shiprocket cancellation failed: {exc}", "error")
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
            from fashion_bot.shiprocket.tools.logistics_adapter import ShiprocketLogisticsAdapter

            logistics_adapter = ShiprocketLogisticsAdapter(client_id=self._resolve_client_id(state))
            update_type = (update_data.get("update_type") or "").lower()

            if update_type == "phone" and update_data.get("new_phone"):
                return await logistics_adapter.aupdate_shipment_phone(
                    order_id,
                    update_data["new_phone"],
                    state=state,
                )

            if update_type == "email" and update_data.get("new_email"):
                return await logistics_adapter.aupdate_shipment_email(
                    order_id,
                    update_data["new_email"],
                    state=state,
                )

            if update_type == "name":
                first_name = update_data.get("first_name", "")
                last_name = update_data.get("last_name", "")
                return await logistics_adapter.aupdate_shipment_name(
                    order_id,
                    first_name,
                    last_name,
                    state=state,
                )

            address_payload: Dict[str, Any] = {}
            new_address = update_data.get("new_address")
            if new_address:
                parts = [part.strip() for part in str(new_address).split("|")]
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
                return await logistics_adapter.aupdate_shipment_address(
                    order_id,
                    address_payload,
                    state=state,
                )

            if update_type in {"instructions", "note", "notes"}:
                return {
                    "success": True,
                    "order_id": order_id,
                    "skipped": True,
                    "message": f"Shiprocket does not support direct '{update_type}' updates via the order adapter.",
                }

            return {
                "success": False,
                "order_id": order_id,
                "error": f"Update type '{update_type or 'unknown'}' is not supported for Shiprocket orders",
            }
        except Exception as exc:
            log_with_trace_id(state, f"Shiprocket update failed: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    async def aget_customer_by_phone(self, phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        try:
            orders = await self.aget_orders_by_customer_phone(phone, limit=5, state=state)
            if not orders:
                return {
                    "found": False,
                    "is_returning_customer": False,
                    "message": "New customer - will need to collect name and address",
                }

            latest_order = max(orders, key=lambda item: item.get("created_at", ""))
            order_data = self._order_to_mapping(latest_order)

            address_parts = [
                order_data.get("billing_address") or order_data.get("customer_address") or "",
                order_data.get("billing_city") or order_data.get("customer_city") or "",
                order_data.get("billing_state") or order_data.get("customer_state") or "",
                order_data.get("billing_pincode") or order_data.get("customer_pincode") or "",
            ]
            customer_address = ", ".join(part for part in address_parts if part)
            customer_name = (
                order_data.get("customer_name")
                or order_data.get("billing_customer_name")
                or order_data.get("billing_name")
                or ""
            )

            return {
                "found": True,
                "is_returning_customer": True,
                "customer_name": customer_name,
                "customer_address": customer_address,
                "message": "Returning customer found",
            }
        except Exception as exc:
            log_with_trace_id(state, f"Shiprocket customer lookup failed: {exc}", "error")
            return {"found": False, "error": str(exc)}

    async def aadd_order_note(
        self,
        order_id: str,
        note: str,
        state: Optional[Dict] = None,
        order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShiprocketAdapter: skipping note add for order {order_id}; Shiprocket has no order note API")
        return {
            "success": True,
            "order_id": order_id,
            "note_added": note,
            "message": "Shiprocket does not support order notes; note skipped",
        }

    async def aadd_order_tags(
        self,
        order_id: str,
        tags: List[str],
        state: Optional[Dict] = None,
        order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShiprocketAdapter: skipping tag add for order {order_id}; Shiprocket has no order tag API")
        return {
            "success": True,
            "order_id": order_id,
            "tags_added": tags,
            "message": "Shiprocket does not support order tags; tags skipped",
        }

    async def aupdate_order_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        from fashion_bot.shiprocket.tools.logistics_adapter import ShiprocketLogisticsAdapter

        logistics_adapter = ShiprocketLogisticsAdapter(client_id=self._resolve_client_id(state))
        return await logistics_adapter.aupdate_shipment_phone(order_id, new_phone, state=state)

    async def aupdate_order_email(self, order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        from fashion_bot.shiprocket.tools.logistics_adapter import ShiprocketLogisticsAdapter

        logistics_adapter = ShiprocketLogisticsAdapter(client_id=self._resolve_client_id(state))
        return await logistics_adapter.aupdate_shipment_email(order_id, new_email, state=state)

    def filter_delivered_orders(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter orders to only include those with 'Delivered' status.
        Shiprocket uses specific status values.
        """
        delivered_statuses = ["delivered", "rto delivered"]
        return [
            order for order in orders
            if (self._order_to_mapping(order).get("partner_status") or self._order_to_mapping(order).get("status") or "").lower() in delivered_statuses
            or (self._order_to_mapping(order).get("shipment_status") or "").lower() in delivered_statuses
        ]

    def format_delivered_orders_for_display(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Format delivered orders for display to the user.
        Uses Shiprocket-specific field names.
        """
        formatted_orders = []
        for order in orders:
            order_data = self._order_to_mapping(order)
            shipments = order_data.get("shipments", {}) or {}
            formatted_order = {
                "order_id": order_data.get("id") or order_data.get("order_id"),
                "channel_order_id": order_data.get("channel_order_id"),
                "awb_code": order_data.get("awb_code") or shipments.get("awb"),
                "status": order_data.get("partner_status") or order_data.get("shipment_status"),
                "delivered_date": order_data.get("delivered_date") or shipments.get("delivered_date") or order_data.get("updated_at"),
                "created_at": order_data.get("created_at"),
                "items": []
            }

            # Extract line items - Shiprocket uses 'products' or 'items'
            line_items = order_data.get("products") or order_data.get("items") or []
            for item in line_items:
                formatted_order["items"].append({
                    "name": item.get("name") or item.get("product_name"),
                    "quantity": item.get("quantity") or item.get("units", 1),
                    "sku": item.get("sku")
                })

            formatted_orders.append(formatted_order)

        return formatted_orders

    def build_delivered_orders_response(self, orders: List[Dict[str, Any]], phone_number: str) -> Dict[str, Any]:
        """
        Build a complete response for delivered orders query.
        """
        delivered_orders = self.filter_delivered_orders(orders)
        formatted_orders = self.format_delivered_orders_for_display(delivered_orders)

        return {
            "success": True,
            "phone_number": phone_number,
            "total_orders": len(orders),
            "delivered_orders_count": len(delivered_orders),
            "delivered_orders": formatted_orders,
            "message": f"Found {len(delivered_orders)} delivered order(s) eligible for return/exchange."
            if delivered_orders else "No delivered orders found for this customer."
        }
