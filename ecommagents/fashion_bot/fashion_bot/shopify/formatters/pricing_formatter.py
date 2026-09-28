"""
Shopify-specific pricing view formatter.
Handles conversion from Shopify OrderInfoDTO to pricing JSON response.
"""
from typing import Dict, Any

def format_pricing_view(order_dto: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format Shopify order data for pricing display.
    
    Args:
        order_dto: Standardized OrderInfoDTO from ShopifyOrderProcessor
    
    Returns:
        Pricing view JSON
    """
    return {
        "order_id": order_dto.get("order_id"),
        "financial_status": order_dto.get("financial_status", "unknown"),
        "created_at": order_dto.get("created_at", "Unknown"),
        "cancelled_at": order_dto.get("cancelled_at"),
        "items": order_dto.get("line_items", []),
        "currency": order_dto.get("currency", "INR"),
        "total_price": order_dto.get("total_price", 0),
        "customer_name": order_dto.get("customer", "Guest")
    }

