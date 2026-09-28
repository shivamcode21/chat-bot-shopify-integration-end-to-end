"""
Mock Enricher for testing purposes.
Implements OrderEnricherInterface with mock enrichment data.
"""
from typing import Dict, Any, Optional, List
from fashion_bot.interfaces.enricher import OrderEnricherInterface


class MockOrderEnricher(OrderEnricherInterface):
    """
    Mock implementation of OrderEnricherInterface for testing.
    Adds mock tracking and status data to orders.
    """
    
    def __init__(self, client_id: str = None, enrichment_type: str = "status"):
        """
        Initialize mock enricher.
        
        Args:
            client_id: Client ID for context
            enrichment_type: Type of enrichment ('status', 'tracking', 'graphql')
        """
        self.client_id = client_id
        self.enrichment_type = enrichment_type
    
    def enrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        Enrich orders with mock data based on enrichment type.
        
        Args:
            orders: List of order DTOs to enrich
            state: Conversation state
            
        Returns:
            List of enriched orders
        """
        if not orders:
            return orders
        
        for order in orders:
            if self.enrichment_type == "status":
                self._enrich_status(order)
            elif self.enrichment_type == "tracking":
                self._enrich_tracking(order)
            elif self.enrichment_type == "graphql":
                self._enrich_graphql(order)
        
        return orders
    
    def _enrich_status(self, order: Dict[str, Any]) -> None:
        """Add mock status enrichment data."""
        current_status = order.get("status", "PENDING")
        
        # Simulate status enrichment
        status_mapping = {
            "PENDING": {
                "partner_status": "Pending",
                "partner_status_code": 1,
                "enriched_status": "Order received, preparing for shipment"
            },
            "SHIPPED": {
                "partner_status": "Shipped",
                "partner_status_code": 4,
                "enriched_status": "Your order has been shipped!"
            },
            "IN_TRANSIT": {
                "partner_status": "In Transit",
                "partner_status_code": 6,
                "enriched_status": "Your order is on its way"
            },
            "OUT_FOR_DELIVERY": {
                "partner_status": "Out for Delivery",
                "partner_status_code": 7,
                "enriched_status": "Your order is out for delivery today!"
            },
            "DELIVERED": {
                "partner_status": "Delivered",
                "partner_status_code": 8,
                "enriched_status": "Your order has been delivered"
            }
        }
        
        enrichment = status_mapping.get(current_status, {
            "partner_status": "Processing",
            "partner_status_code": 0,
            "enriched_status": "Order is being processed"
        })
        
        order.update(enrichment)
        order["enriched_by"] = "mock_status_enricher"
    
    def _enrich_tracking(self, order: Dict[str, Any]) -> None:
        """Add mock tracking enrichment data."""
        order_id = order.get("order_id", "MOCK")
        
        # Add mock tracking data
        order["awb"] = order.get("awb") or f"MOCK{order_id}12345"
        order["courier"] = order.get("courier") or "Mock Express"
        order["tracking_url"] = f"https://mock-tracking.com/{order.get('awb')}"
        order["last_tracking_update"] = {
            "date": "2025-12-10 14:00:00",
            "status": "In Transit",
            "location": "Mock Hub - Bangalore",
            "activity": "Shipment arrived at destination hub"
        }
        order["enriched_by"] = "mock_tracking_enricher"
    
    def _enrich_graphql(self, order: Dict[str, Any]) -> None:
        """Add mock GraphQL enrichment data (line items, etc.)."""
        # Ensure line_items exist
        if not order.get("line_items"):
            order["line_items"] = [
                {
                    "id": "gid://shopify/LineItem/12345",
                    "name": "Mock Product - Cosmic Shacket",
                    "quantity": 1,
                    "price": "2999.00",
                    "variant_title": "M",
                    "image": {
                        "url": "https://example.com/mock-product.jpg",
                        "alt": "Mock Product Image"
                    }
                }
            ]
        
        # Add fulfillment details
        order["fulfillment_details"] = order.get("fulfillment_details") or {
            "status": order.get("fulfillment_status", "pending"),
            "tracking_company": order.get("courier", "Mock Express"),
            "tracking_number": order.get("awb", "MOCK12345"),
            "tracking_url": f"https://mock-tracking.com/{order.get('awb', 'MOCK12345')}"
        }
        
        order["enriched_by"] = "mock_graphql_enricher"


class MockStatusEnricher(MockOrderEnricher):
    """Mock enricher for status enrichment (Shiprocket-style)."""
    
    def __init__(self, client_id: str = None):
        super().__init__(client_id=client_id, enrichment_type="status")


class MockTrackingEnricher(MockOrderEnricher):
    """Mock enricher for tracking enrichment."""
    
    def __init__(self, client_id: str = None):
        super().__init__(client_id=client_id, enrichment_type="tracking")


class MockGraphQLEnricher(MockOrderEnricher):
    """Mock enricher for GraphQL-style enrichment (line items, images)."""
    
    def __init__(self, client_id: str = None):
        super().__init__(client_id=client_id, enrichment_type="graphql")

