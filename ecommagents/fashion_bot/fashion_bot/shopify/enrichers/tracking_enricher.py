from typing import List, Dict, Any, Optional
from fashion_bot.interfaces.enricher import OrderEnricherInterface
from fashion_bot.core.factory import ServiceFactory
from fashion_bot.utils.utils import log_with_trace_id

class ShopifyTrackingEnricher(OrderEnricherInterface):
    """
    Enriches orders with Shopify tracking data (for orders missing tracking URL).
    """
    def enrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        raise RuntimeError("ShopifyTrackingEnricher is async-only. Use `await aenrich(...)`.")

    async def aenrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        log_with_trace_id(state, "Enriching orders with Shopify tracking data...")
        
        shopify_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        shopify_processor = ServiceFactory.get_order_processor("shopify")
        
        for order_info in orders:
            if not order_info.get('tracking_url') and order_info.get('channel_order_id'):
                try:
                    shopify_order = await shopify_service.aget_order_details(order_info['channel_order_id'], state=state)
                    if shopify_order:
                        shopify_dto = shopify_processor.process_order(shopify_order, state=state)
                        if shopify_dto.get('tracking_url'):
                            order_info['tracking_url'] = shopify_dto['tracking_url']
                            order_info['courier'] = shopify_dto['courier'] or order_info.get('courier')
                            order_info['awb'] = shopify_dto['awb'] or order_info.get('awb')
                            log_with_trace_id(state, "Enriched with Shopify tracking")
                except Exception as e:
                    log_with_trace_id(state, f"Tracking enrichment failed for {order_info.get('order_id')}: {e}", "warning")
                    
        return orders
