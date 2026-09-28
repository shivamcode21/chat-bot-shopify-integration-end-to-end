"""
Helper module to get current line items for an order using GraphQL Admin API.
This is necessary because the REST API does not reflect Order Edit changes correctly.
"""

from typing import List
from fashion_bot.config_manager import aget_shopify_config
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.utils.http_client import get_shared_async_http_client


def get_current_line_items_graphql(order_id: str, state: dict = None, client_id: str = None) -> List[str]:
    raise RuntimeError(
        "get_current_line_items_graphql is async-only. Use `await aget_current_line_items_graphql(...)`."
    )


async def aget_current_line_items_graphql(order_id: str, state: dict = None, client_id: str = None) -> List[str]:
    """
    Get the current active line items for an order using GraphQL Admin API.
    
    This function fetches the current state of line items AFTER any Order Edits,
    which the REST API does not accurately reflect.
    
    Args:
        order_id: Shopify order ID (numeric, e.g., "6130237530434")
        state: State dict for logging
        client_id: Client ID for multi-tenant support (optional)
        
    Returns:
        List of line item names (only active items with quantity > 0)
    """
    try:
        log_with_trace_id(state, f"🔍 GraphQL: Fetching current line items for order ID: {order_id}")
        
        # Get Shopify config
        if not client_id:
            client_id = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"  # Default Groovee client
        shopify_config = await aget_shopify_config(client_id=client_id)
        
        shop_url = shopify_config.get('shop_url')
        access_token = shopify_config.get('access_token')
        
        # GraphQL query to get current line items
        query = """
        query getOrder($id: ID!) {
          order(id: $id) {
            id
            name
            lineItems(first: 100) {
              edges {
                node {
                  id
                  name
                  quantity
                  variant {
                    id
                    title
                  }
                }
              }
            }
          }
        }
        """
        
        # Convert order ID to GID format
        gid = f"gid://shopify/Order/{order_id}"
        
        variables = {
            "id": gid
        }
        
        url = f"https://{shop_url}/admin/api/2024-10/graphql.json"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }
        
        client = await get_shared_async_http_client()
        response = await client.post(
            url,
            json={"query": query, "variables": variables},
            headers=headers
        )
        
        if response.status_code != 200:
            log_with_trace_id(state, f"GraphQL API error: {response.status_code} - {response.text}", "error")
            return []
        
        data = response.json()
        
        if "errors" in data:
            log_with_trace_id(state, f"GraphQL query errors: {data['errors']}", "error")
            return []
        
        # Extract line items
        order = data.get("data", {}).get("order")
        if not order:
            log_with_trace_id(state, f"Order {order_id} not found in GraphQL response", "warning")
            return []
        
        line_items_edges = order.get("lineItems", {}).get("edges", [])
        
        # Filter items with quantity > 0 (active items only)
        active_items = []
        for edge in line_items_edges:
            node = edge.get("node", {})
            quantity = node.get("quantity", 0)
            name = node.get("name", "Unknown Item")
            
            log_with_trace_id(state, f"🔍 GraphQL line item: {name}, quantity: {quantity}")
            
            if quantity > 0:
                active_items.append(name)
                log_with_trace_id(state, f"✅ Active item: {name} (quantity: {quantity})")
            else:
                log_with_trace_id(state, f"⏭️ Skipping removed item: {name} (quantity: {quantity})")
        
        log_with_trace_id(state, f"📊 Order {order_id}: {len(active_items)} active items out of {len(line_items_edges)} total")
        return active_items
        
    except Exception as e:
        log_with_trace_id(state, f"Error fetching current line items via GraphQL: {str(e)}", "error")
        return []
