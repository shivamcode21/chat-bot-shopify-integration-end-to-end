"""
Mock Order Processor for testing purposes.
Implements OrderProcessorInterface with standardized mock data transformation.
"""
from typing import Dict, Any, Optional, List
from datetime import datetime
from fashion_bot.interfaces.processor import OrderProcessorInterface


class MockOrderProcessor(OrderProcessorInterface):
    """Mock implementation of OrderProcessorInterface for testing."""
    
    def __init__(self, client_id: str = None):
        self.client_id = client_id
    
    def process_order(self, raw_data: Any, state: Optional[Dict] = None, **kwargs) -> Dict[str, Any]:
        """
        Convert raw mock order data to standardized OrderInfoDTO format.
        
        Args:
            raw_data: The raw order data (dict or object)
            state: Conversation state for logging/context
            **kwargs: Additional context
            
        Returns:
            Dict matching the OrderInfoDTO structure
        """
        if not raw_data:
            return {}
        
        # Handle both dict and object-like data
        if hasattr(raw_data, 'get'):
            data = raw_data
        elif hasattr(raw_data, '__dict__'):
            data = raw_data.__dict__
        else:
            data = dict(raw_data)
        
        # Extract order ID - support multiple formats
        order_id = (
            data.get('name') or 
            data.get('order_name') or 
            data.get('order_id') or 
            data.get('id', 'MOCK-ORDER')
        )
        
        # Clean order ID (remove # prefix if present)
        if isinstance(order_id, str):
            order_id_clean = order_id.lstrip('#')
        else:
            order_id_clean = str(order_id)
        
        # Determine status
        fulfillment_status = data.get('fulfillment_status', 'pending')
        financial_status = data.get('financial_status', 'paid')
        
        # Map to standardized status
        status = self._map_status(fulfillment_status, financial_status)
        
        # Build standardized DTO
        return {
            "order_id": order_id_clean,
            "order_name": data.get('name', f"#{order_id_clean}"),
            "status": status,
            "fulfillment_status": fulfillment_status,
            "financial_status": financial_status,
            "created_at": data.get('created_at', datetime.now().isoformat()),
            "total_price": data.get('total_price', '0.00'),
            "currency": data.get('currency', 'INR'),
            "customer_name": self._extract_customer_name(data),
            "customer_email": data.get('email', 'mock@example.com'),
            "customer_phone": self._extract_customer_phone(data),
            "shipping_address": self._extract_shipping_address(data),
            "line_items": data.get('line_items', []),
            "items": self._extract_item_names(data),
            "awb": data.get('awb') or data.get('awb_code'),
            "courier": data.get('courier') or data.get('courier_name'),
            "delivery_date": data.get('delivery_date') or data.get('edd'),
            "source": "mock"
        }
    
    def process_orders(self, raw_orders: List[Any], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        Process a list of raw orders into standardized DTOs.
        
        Args:
            raw_orders: List of raw order data
            state: Conversation state
            
        Returns:
            List of standardized OrderInfoDTOs
        """
        if not raw_orders:
            return []
        
        processed = []
        for order in raw_orders:
            try:
                dto = self.process_order(order, state)
                if dto:
                    processed.append(dto)
            except Exception:
                # Skip invalid orders in mock mode
                continue
        
        return processed
    
    def _map_status(self, fulfillment_status: str, financial_status: str) -> str:
        """Map fulfillment/financial status to a unified status string."""
        fulfillment_lower = (fulfillment_status or '').lower()
        financial_lower = (financial_status or '').lower()
        
        # Priority: fulfillment status
        status_map = {
            'fulfilled': 'DELIVERED',
            'delivered': 'DELIVERED',
            'partial': 'PARTIALLY_FULFILLED',
            'in_transit': 'IN_TRANSIT',
            'shipped': 'SHIPPED',
            'out_for_delivery': 'OUT_FOR_DELIVERY',
            'pending': 'PENDING',
            'unfulfilled': 'PENDING',
            'cancelled': 'CANCELLED',
            'refunded': 'REFUNDED'
        }
        
        if fulfillment_lower in status_map:
            return status_map[fulfillment_lower]
        
        # Check financial status for cancelled/refunded
        if financial_lower in ['refunded', 'voided']:
            return 'CANCELLED'
        
        return 'PENDING'
    
    def _extract_customer_name(self, data: Dict) -> str:
        """Extract customer name from order data."""
        # Try different fields
        if data.get('customer'):
            customer = data['customer']
            if isinstance(customer, dict):
                first = customer.get('first_name', '')
                last = customer.get('last_name', '')
                return f"{first} {last}".strip() or customer.get('name', 'Mock Customer')
            return str(customer)
        
        if data.get('shipping_address'):
            addr = data['shipping_address']
            if isinstance(addr, dict):
                first = addr.get('first_name', '')
                last = addr.get('last_name', '')
                return f"{first} {last}".strip() or addr.get('name', 'Mock Customer')
        
        return data.get('customer_name', 'Mock Customer')
    
    def _extract_customer_phone(self, data: Dict) -> str:
        """Extract customer phone from order data."""
        # Try different fields in priority order
        phone_fields = [
            'phone',
            'customer_phone',
            'billing_phone',
            'shipping_phone'
        ]
        
        for field in phone_fields:
            if data.get(field):
                return data[field]
        
        # Try nested shipping_address
        if data.get('shipping_address') and isinstance(data['shipping_address'], dict):
            return data['shipping_address'].get('phone', '+919876543210')
        
        return '+919876543210'
    
    def _extract_shipping_address(self, data: Dict) -> Dict[str, str]:
        """Extract and normalize shipping address."""
        if data.get('shipping_address'):
            addr = data['shipping_address']
            if isinstance(addr, dict):
                return {
                    "name": addr.get('name', ''),
                    "address1": addr.get('address1', ''),
                    "address2": addr.get('address2', ''),
                    "city": addr.get('city', ''),
                    "province": addr.get('province', addr.get('state', '')),
                    "zip": addr.get('zip', addr.get('pincode', '')),
                    "country": addr.get('country', 'India'),
                    "phone": addr.get('phone', '')
                }
            return {"address1": str(addr)}
        
        return {
            "name": "Mock Customer",
            "address1": "123 Mock Street",
            "city": "Mumbai",
            "province": "Maharashtra",
            "zip": "400001",
            "country": "India",
            "phone": "+919876543210"
        }
    
    def _extract_item_names(self, data: Dict) -> List[str]:
        """Extract item names from line_items."""
        items = []
        line_items = data.get('line_items', [])
        
        for item in line_items:
            if isinstance(item, dict):
                name = item.get('name') or item.get('title', 'Unknown Product')
                items.append(name)
            else:
                items.append(str(item))
        
        return items if items else ["Mock Product"]

