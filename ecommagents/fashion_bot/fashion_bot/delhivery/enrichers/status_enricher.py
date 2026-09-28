"""
DelhiveryStatusEnricher — for the top N Shopify orders, pulls Delhivery's
view to populate `partner_status` and re-classify status. Mirrors
ShiprocketStatusEnricher; disabled by default in `vendor_config.py`.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fashion_bot.core.factory import ServiceFactory
from fashion_bot.interfaces.enricher import OrderEnricherInterface
from fashion_bot.tools import classify_status
from fashion_bot.utils.utils import log_with_trace_id


class DelhiveryStatusEnricher(OrderEnricherInterface):
    """Async-only enricher that fills `partner_status` from Delhivery."""

    def enrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        raise RuntimeError("DelhiveryStatusEnricher is async-only. Use `await aenrich(...)`.")

    async def aenrich(
        self,
        orders: List[Dict[str, Any]],
        state: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        log_with_trace_id(state, "Enriching orders with Delhivery status...")

        # Optimisation: only check Top 3 most recent orders
        sorted_indices = sorted(
            range(len(orders)),
            key=lambda i: orders[i].get("created_at", ""),
            reverse=True,
        )
        top_n = int(os.getenv("DELHIVERY_ENRICH_TOP_N", "3"))
        candidates = set(sorted_indices[:top_n])

        dl_service = await ServiceFactory.aget_order_service(state=state, vendor="delhivery")
        dl_processor = ServiceFactory.get_order_processor("delhivery")

        for i in candidates:
            if i >= len(orders):
                continue
            order = orders[i]
            order_name = order.get("channel_order_id") or order.get("order_id")
            if "status_source" not in order:
                order["status_source"] = order.get("source", "shopify")

            try:
                result = await dl_service.aget_order_details(order_name, state=state)
                raw_orders = result.get("orders", [])
                if not raw_orders:
                    continue
                dtos = dl_processor.process_orders(raw_orders, state=state)
                if not dtos:
                    continue
                dto = dtos[0]
                order["partner_status"] = dto.get("partner_status")
                order["partner_name"] = "delhivery"
                order["status_source"] = "delhivery"

                is_cancelled = order.get("status") == "Cancelled"
                order["status"] = classify_status(
                    dto.get("partner_status"),
                    "cancelled" if is_cancelled else None,
                    order.get("fulfillment_status"),
                )
                log_with_trace_id(
                    state,
                    f"✅ Delhivery status found for {order_name}: {dto.get('partner_status')}",
                )
            except Exception as exc:
                log_with_trace_id(
                    state,
                    f"Delhivery enrichment failed for {order_name}: {exc}",
                    "warning",
                )

        return orders
