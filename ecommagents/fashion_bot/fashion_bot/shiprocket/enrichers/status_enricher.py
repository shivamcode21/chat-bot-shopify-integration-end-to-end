from typing import List, Dict, Any, Optional
from fashion_bot.interfaces.enricher import OrderEnricherInterface
from fashion_bot.core.factory import ServiceFactory
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.tools import classify_status

class ShiprocketStatusEnricher(OrderEnricherInterface):
    def enrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        raise RuntimeError("ShiprocketStatusEnricher is async-only. Use `await aenrich(...)`.")

    async def aenrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        log_with_trace_id(state, "Enriching orders with Shiprocket status...")
        
        # Optimization: Only check Top 3 most recent orders
        sorted_indices = sorted(range(len(orders)), key=lambda i: orders[i].get('created_at', ''), reverse=True)
        top_3_indices = set(sorted_indices[:3])
        
        sr_service = await ServiceFactory.aget_order_service(state=state, vendor="shiprocket")
        sr_processor = ServiceFactory.get_order_processor("shiprocket")
        
        for i in top_3_indices:
            if i >= len(orders):
                continue
                
            order = orders[i]
            # Use channel_order_id (e.g., GV12178) for Shiprocket lookups, not the Shopify internal ID
            order_name = order.get('channel_order_id') or order.get('order_id')
            
            # Initialize fields if not present
            if 'status_source' not in order:
                order['status_source'] = order.get('source', 'shopify')  # Default to primary vendor
            
            try:
                sr_result = await sr_service.aget_order_details(order_name, state=state)
                sr_raw_orders = sr_result.get('orders', [])
                
                if sr_raw_orders:
                    sr_dtos = sr_processor.process_orders(sr_raw_orders, state=state)
                    if sr_dtos:
                        sr_dto = sr_dtos[0]
                        order['partner_status'] = sr_dto['partner_status']
                        order['shiprocket_raw_status'] = sr_dto['partner_status']
                        order['status_source'] = "shiprocket"
                        
                        # Re-derive status
                        is_cancelled = order.get('status') == "Cancelled"
                        order['status'] = classify_status(
                            sr_dto['partner_status'], 
                            "cancelled" if is_cancelled else None, 
                            order.get('fulfillment_status')
                        )
                        log_with_trace_id(state, f"✅ Shiprocket status found for {order_name}: {sr_dto['partner_status']}")
            except Exception as e:
                log_with_trace_id(state, f"Shiprocket enrichment failed for {order_name}: {e}", "warning")
                
        return orders
