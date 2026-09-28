"""
ClickPostOrderProcessor -- turns the rows ``aget_matching_orders`` builds
into the same DTO the other partners' processors emit.

This runs on the winning partner's rows in the order-status race, and its
output *is* the answer: the orchestrator returns ``len(processed_orders)``
directly, with no fallback below it. Dropping a row here therefore reports a
real order as "not found" to every update and cancel tool, so each row the
adapter found must come back out.

All DTO assembly lives in
``core.partner_response_mappings.build_order_info_dto_from_canonical``; the
``order_data`` on each row is already canonical (the adapter maps it through
``GET_ORDER_DATA_FIELD_MAPS``), so it is passed straight through rather than
re-mapped.
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

PARTNER_NAME = "clickpost"


class ClickPostOrderProcessor(OrderProcessorInterface):
    """Process ClickPost rows into the canonical OrderInfoDTO."""

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
            log_with_trace_id(state, f"Error processing ClickPost order: {exc}", "error")
            return {}

    def process_orders(self, raw_orders: List[Any], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        processed = []
        for order in raw_orders:
            res = self.process_order(order, state)
            if res:
                processed.append(res)
        return processed
