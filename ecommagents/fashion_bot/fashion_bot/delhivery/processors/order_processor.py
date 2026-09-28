"""
DelhiveryOrderProcessor — turns raw Delhivery shipment payloads (already
normalised by ``DelhiveryLogisticsAdapter._shipment_to_order_data`` into the
canonical order_data shape) into the same DTO that ShopifyOrderProcessor /
ShiprocketOrderProcessor emit.

All DTO-assembly logic lives in
``fashion_bot.core.partner_response_mappings.build_order_info_dto_from_canonical``
so adding a new partner does not require duplicating this file.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fashion_bot.core.partner_response_mappings import (
    build_order_info_dto_from_canonical,
)
from fashion_bot.interfaces.processor import OrderProcessorInterface
from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)

PARTNER_NAME = "delhivery"


class DelhiveryOrderProcessor(OrderProcessorInterface):
    """Process Delhivery raw orders into the canonical OrderInfoDTO."""

    @staticmethod
    def _coerce_order(order: Any) -> Any:
        if isinstance(order, dict) and "orders" in order:
            nested = order.get("orders") or []
            return nested[0] if nested else None
        return order

    def process_order(self, order: Any, state: Optional[Dict] = None, **kwargs) -> Dict[str, Any]:
        try:
            order = self._coerce_order(order)
            if not order:
                return {}

            if isinstance(order, dict):
                order_data = order.get("order_data", {}) or order
                raw_status = (
                    order.get("partner_status")
                    or order.get("status")
                    or order.get("shipment_status")
                    or ""
                )
                shipment_status_value = order.get("shipment_status") or raw_status
                order_id_value = (
                    order.get("order_id") or order.get("id") or order.get("name")
                )
                channel_order_id_value = (
                    order.get("channel_order_id") or order.get("name") or order_id_value
                )
            else:
                order_data = getattr(order, "order_data", {})
                raw_status = getattr(order, "status", "")
                shipment_status_value = getattr(order, "shipment_status", "")
                order_id_value = getattr(order, "order_id", "")
                channel_order_id_value = getattr(order, "channel_order_id", "")

            return build_order_info_dto_from_canonical(
                partner=PARTNER_NAME,
                order_id=order_id_value,
                channel_order_id=channel_order_id_value,
                raw_status=raw_status,
                shipment_status=shipment_status_value,
                order_data=order_data,
            )

        except Exception as exc:
            log_with_trace_id(state, f"Error processing Delhivery order: {exc}", "error")
            return {}

    def process_orders(self, raw_orders: List[Any], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        processed = []
        for order in raw_orders:
            res = self.process_order(order, state)
            if res:
                processed.append(res)
        return processed

    @staticmethod
    def format_delivered_orders_for_llm(formatted_orders: List[Dict[str, Any]]) -> str:
        if not formatted_orders:
            return "No delivered orders found for this customer."
        count = len(formatted_orders)
        lines = [f"Found {count} delivered order(s) eligible for return/exchange:\n"]
        for i, order in enumerate(formatted_orders, 1):
            order_name = order.get("order_name") or order.get("order_id") or "Unknown"
            delivered_date = order.get("delivered_date") or ""
            items = order.get("items", [])
            lines.append(f"{i}. Order {order_name}")
            if delivered_date:
                date_display = delivered_date[:10] if len(str(delivered_date)) > 10 else str(delivered_date)
                lines.append(f"   Delivered: {date_display}")
            if items:
                names = [it.get("name") or it.get("title") or "Unknown item" for it in items[:3]]
                if len(items) > 3:
                    names.append(f"...and {len(items) - 3} more")
                lines.append(f"   Items: {', '.join(names)}")
            lines.append("")
        if count == 1:
            lines.append("Ask customer to confirm if they want to return/exchange this order.")
        else:
            lines.append("Ask customer which order number they want to return/exchange.")
        return "\n".join(lines)
