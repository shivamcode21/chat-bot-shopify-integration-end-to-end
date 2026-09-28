"""
Mock Order Adapter for testing purposes.
Implements OrderInterface with mock data.
"""
import json
import os
import logging
from typing import Dict, Any, Optional, List
from fashion_bot.interfaces.order import OrderInterface

logger = logging.getLogger("fashion_bot.mock.order")


def _log_mock_call(method: str, params: str = ""):
    """Log mock adapter method calls for debugging."""
    msg = f"[MOCK CALL] 📦 MockOrderAdapter.{method}({params})"
    logger.info(msg)
    if os.getenv("DEBUG_FACTORY_ROUTES", "").lower() == "true":
        print(msg)


class MockOrderAdapter(OrderInterface):
    """Mock implementation of OrderInterface for testing."""
    
    def __init__(self, client_id: str = None):
        self.client_id = client_id
        self._mock_data_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 
            "data"
        )
        _log_mock_call("__init__", f"client_id={client_id}")
    
    def _load_mock_data(self, vendor: str, method: str, key: str = None) -> Dict[str, Any]:
        """
        Load mock data from JSON files.
        Priority: client-specific > default
        """
        # Try client-specific data first
        if self.client_id:
            client_path = os.path.join(
                self._mock_data_dir, "clients", "groovee", vendor, f"{method}.json"
            )
            if os.path.exists(client_path):
                with open(client_path, 'r') as f:
                    data = json.load(f)
                    if key and "mapping" in data:
                        scenario = data["mapping"].get(key, data.get("default_scenario", "default"))
                        return data.get("scenarios", {}).get(scenario, {})
                    elif "scenarios" in data:
                        default_scenario = data.get("default_scenario", list(data["scenarios"].keys())[0])
                        return data.get("scenarios", {}).get(default_scenario, {})
                    return data
        
        # Fall back to default data
        default_path = os.path.join(self._mock_data_dir, "default", vendor, f"{method}.json")
        if os.path.exists(default_path):
            with open(default_path, 'r') as f:
                return json.load(f)
        
        return {}

    def get_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Get mock order details by order ID."""
        _log_mock_call("get_order_details", f"order_id={order_id}")
        mock_data = self._load_mock_data("shopify", "get_order_details", order_id)
        
        if mock_data and "order" in mock_data:
            return mock_data["order"]
        
        # Default mock order
        return {
            "id": 12345,
            "name": f"#{order_id}",
            "email": "mock@example.com",
            "fulfillment_status": "fulfilled",
            "financial_status": "paid",
            "total_price": "2999.00",
            "currency": "INR",
            "created_at": "2025-12-01T10:30:00Z",
            "line_items": [
                {
                    "id": 1,
                    "name": "Mock Product - Cosmic Shacket",
                    "quantity": 1,
                    "price": "2999.00",
                    "variant_title": "M"
                }
            ],
            "shipping_address": {
                "first_name": "Rahul",
                "last_name": "Sharma",
                "address1": "123 Mock Street",
                "city": "Mumbai",
                "province": "Maharashtra",
                "zip": "400001",
                "country": "India",
                "phone": "+919876543210"
            },
            "customer": {
                "first_name": "Rahul",
                "last_name": "Sharma",
                "email": "mock@example.com",
                "phone": "+919876543210"
            }
        }

    def get_orders_by_customer_phone(self, phone: str, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Get mock orders for a customer by phone number."""
        _log_mock_call("get_orders_by_customer_phone", f"phone={phone}, limit={limit}")
        mock_data = self._load_mock_data("shopify", "get_orders_by_phone", phone)
        
        if mock_data and "orders" in mock_data:
            orders = mock_data["orders"]
            return orders[:limit] if limit else orders
        
        # Default mock orders
        return [
            {
                "id": 12345,
                "name": "#GRV1001",
                "order_number": 1001,
                "email": "customer@example.com",
                "created_at": "2025-12-01T10:30:00Z",
                "financial_status": "paid",
                "fulfillment_status": "fulfilled",
                "total_price": "2999.00",
                "currency": "INR",
                "line_items": [{"name": "Mock Product A", "quantity": 1}],
                "shipping_address": {
                    "first_name": "Test",
                    "last_name": "Customer",
                    "phone": phone
                }
            },
            {
                "id": 12346,
                "name": "#GRV1002",
                "order_number": 1002,
                "email": "customer@example.com",
                "created_at": "2025-12-06T09:00:00Z",
                "financial_status": "paid",
                "fulfillment_status": "pending",
                "total_price": "1499.00",
                "currency": "INR",
                "line_items": [{"name": "Mock Product B", "quantity": 1}],
                "shipping_address": {
                    "first_name": "Test",
                    "last_name": "Customer",
                    "phone": phone
                }
            }
        ][:limit]

    def create_order(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a mock order."""
        _log_mock_call("create_order", f"order_data_keys={list(order_data.keys())}")
        return {
            "id": 999999,
            "name": "#MOCK-NEW",
            "status": "created",
            "financial_status": "pending",
            "fulfillment_status": "unfulfilled",
            "total_price": order_data.get("total_price", "0.00"),
            "message": "Mock order created successfully"
        }

    async def acreate_order_multi(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a single mock order containing multiple line items."""
        items = order_data.get("items") or []
        _log_mock_call("acreate_order_multi", f"item_count={len(items)}")
        return {
            "success": True,
            "order_id": "#MOCK-NEW",
            "order_number": 999999,
            "financial_status": "pending",
            "fulfillment_status": "unfulfilled",
            "currency": "INR",
            "item_count": len(items),
            "line_items": [
                {
                    "title": it.get("product_link", ""),
                    "quantity": it.get("quantity", 1),
                    "price": "0.00",
                }
                for it in items
            ],
            "message": f"Mock multi-item order created successfully with {len(items)} item(s)",
        }

    def cancel_order(
        self,
        order_id: str,
        reason: str = "",
        state: Optional[Dict] = None,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        """Cancel a mock order."""
        _log_mock_call(
            "cancel_order",
            f"order_id={order_id}, reason={reason}, custom_note={custom_note}, skip_refund={skip_refund}",
        )
        return {
            "success": True,
            "id": order_id,
            "status": "cancelled",
            "reason": reason,
            "message": f"Mock order {order_id} cancelled successfully"
        }

    def update_order(self, order_id: str, update_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Update a mock order."""
        _log_mock_call("update_order", f"order_id={order_id}, update_keys={list(update_data.keys())}")
        return {
            "success": True,
            "id": order_id,
            "status": "updated",
            "updated_fields": list(update_data.keys()),
            "message": f"Mock order {order_id} updated successfully"
        }

    def filter_delivered_orders(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter orders to only include those with confirmed DELIVERED status.
        fulfilled != delivered. fulfilled means shipped/dispatched."""
        delivered_statuses = ['delivered', 'rto delivered']
        return [
            order for order in orders
            if (order.get('partner_status', '').lower() in delivered_statuses or
                order.get('shipment_status', '').lower() in delivered_statuses or
                order.get('status', '').lower() in delivered_statuses or
                order.get('delivered_date'))
        ]

    def format_delivered_orders_for_display(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Format delivered orders for display to the user."""
        formatted = []
        for order in orders:
            formatted.append({
                "order_id": order.get('name', order.get('id')),
                "total": order.get('total_price', '0.00'),
                "currency": order.get('currency', 'INR'),
                "items": [item.get('name', 'Product') for item in order.get('line_items', [])],
                "delivered_date": order.get('delivered_at', order.get('updated_at', 'Recently'))
            })
        return formatted

    def build_delivered_orders_response(self, orders: List[Dict[str, Any]], phone_number: str) -> Dict[str, Any]:
        """Build a complete response for delivered orders query."""
        delivered = self.filter_delivered_orders(orders)
        formatted = self.format_delivered_orders_for_display(delivered)
        
        return {
            "success": True,
            "phone_number": phone_number,
            "total_delivered": len(formatted),
            "orders": formatted,
            "message": f"Found {len(formatted)} delivered orders for {phone_number}"
        }

    def get_customer_by_phone(self, phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Get mock customer details by phone number."""
        # Check if we have orders for this phone
        orders = self.get_orders_by_customer_phone(phone, limit=1, state=state)
        
        if orders:
            order = orders[0]
            shipping = order.get('shipping_address', {})
            customer = order.get('customer', {})
            
            return {
                "found": True,
                "customer_id": customer.get('id', 12345),
                "first_name": shipping.get('first_name') or customer.get('first_name', 'Mock'),
                "last_name": shipping.get('last_name') or customer.get('last_name', 'Customer'),
                "email": order.get('email', 'mock@example.com'),
                "phone": phone,
                "orders_count": len(orders),
                "address": {
                    "address1": shipping.get('address1', '123 Mock Street'),
                    "city": shipping.get('city', 'Mumbai'),
                    "province": shipping.get('province', 'Maharashtra'),
                    "zip": shipping.get('zip', '400001'),
                    "country": shipping.get('country', 'India')
                }
            }
        
        return {
            "found": False,
            "message": f"No customer found with phone {phone}"
        }

    def add_order_note(self, order_id: str, note: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Add a note to a mock order."""
        return {
            "success": True,
            "order_id": order_id,
            "note_added": note,
            "message": f"Note added to mock order {order_id}"
        }

    def add_order_tags(self, order_id: str, tags: List[str], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Add tags to a mock order."""
        return {
            "success": True,
            "order_id": order_id,
            "tags_added": tags,
            "message": f"Tags added to mock order {order_id}"
        }

    def update_order_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Update phone number for a mock order."""
        return {
            "success": True,
            "order_id": order_id,
            "new_phone": new_phone,
            "message": f"Phone updated for mock order {order_id}"
        }

    def update_order_email(self, order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Update email address for a mock order."""
        return {
            "success": True,
            "order_id": order_id,
            "new_email": new_email,
            "message": f"Email updated for mock order {order_id}"
        }

    def clone_order(
        self,
        original_order_data: Dict[str, Any],
        new_line_items: list,
        state: Optional[Dict] = None,
        note: str = "",
        additional_tags: Optional[list] = None,
        financial_status_override: Optional[str] = None,
        payment_gateway_names_override: Optional[list] = None,
        transactions: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Clone a mock order with new line items."""
        _log_mock_call("clone_order", f"original={original_order_data.get('name', 'N/A')}")
        original_name = original_order_data.get("name", "#0000")
        mock_order = {
            "id": 9999999,
            "name": f"#CLONE-{original_name.lstrip('#')}",
            "order_number": 9999999,
            "total_price": "0.00",
            "currency": original_order_data.get("currency", "INR"),
            "financial_status": financial_status_override or original_order_data.get("financial_status", "pending"),
            "fulfillment_status": None,
            "shipping_address": original_order_data.get("shipping_address", {}),
            "billing_address": original_order_data.get("billing_address", {}),
            "customer": original_order_data.get("customer", {}),
            "email": original_order_data.get("email", ""),
            "phone": original_order_data.get("phone", ""),
            "tags": original_order_data.get("tags", ""),
            "note": note or original_order_data.get("note", ""),
            "note_attributes": original_order_data.get("note_attributes", []),
            "discount_codes": original_order_data.get("discount_codes", []),
            "line_items": new_line_items,
        }
        return {
            "success": True,
            "order": mock_order,
            "order_id": mock_order["name"],
            "order_name": mock_order["name"],
            "order_number": str(mock_order["order_number"]),
            "total_price": mock_order["total_price"],
            "financial_status": mock_order["financial_status"],
        }

    def refund_order(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Issue a mock refund for a cancelled order."""
        _log_mock_call("refund_order", f"order_id={order_id}")
        return {
            "success": True,
            "order_id": order_id,
            "refund_id": 99999,
            "message": f"Mock refund processed for order {order_id}",
        }
