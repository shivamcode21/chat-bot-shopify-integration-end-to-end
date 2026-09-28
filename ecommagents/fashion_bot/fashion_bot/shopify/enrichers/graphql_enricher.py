from typing import List, Dict, Any, Optional
from fashion_bot.interfaces.enricher import OrderEnricherInterface
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.tools import _get_client_id_from_state

class ShopifyGraphQLEnricher(OrderEnricherInterface):
    def enrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        raise RuntimeError("ShopifyGraphQLEnricher is async-only. Use `await aenrich(...)`.")

    async def aenrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        log_with_trace_id(state, "Enriching orders with Shopify GraphQL data...")
        client_id = _get_client_id_from_state(state)
        
        # Optimization: Only check Top 5 most recent orders
        sorted_indices = sorted(range(len(orders)), key=lambda i: orders[i].get('created_at', ''), reverse=True)
        top_5_indices = set(sorted_indices[:5])
        
        for i in top_5_indices:
            order = orders[i]
            order_name = order.get('order_id')
            channel_id = order.get('channel_order_id')
            
            if not channel_id:
                continue
                
            try:
                from fashion_bot.shopify.modules.get_current_line_items_graphql import aget_current_line_items_graphql

                graphql_items = await aget_current_line_items_graphql(channel_id, state, client_id)
                order['items'] = graphql_items
                log_with_trace_id(state, f"✅ GraphQL items fetched for {order_name}")
            except Exception as e:
                log_with_trace_id(state, f"GraphQL enrichment failed for {order_name}: {e}", "warning")
                
        return orders
