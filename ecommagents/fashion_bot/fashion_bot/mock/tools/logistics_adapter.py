"""
Mock Logistics Adapter for testing purposes.
Implements LogisticsInterface with mock data.
"""
import json
import os
import logging
from typing import Dict, Any, Optional, List
from fashion_bot.interfaces.logistics import LogisticsInterface

logger = logging.getLogger("fashion_bot.mock.logistics")


def _log_mock_call(method: str, params: str = ""):
    """Log mock adapter method calls for debugging."""
    msg = f"[MOCK CALL] 🚚 MockLogisticsAdapter.{method}({params})"
    logger.info(msg)
    if os.getenv("DEBUG_FACTORY_ROUTES", "").lower() == "true":
        print(msg)


class MockLogisticsAdapter(LogisticsInterface):
    """Mock implementation of LogisticsInterface for testing."""
    
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
                    return data
        
        # Fall back to default data
        default_path = os.path.join(self._mock_data_dir, "default", vendor, f"{method}.json")
        if os.path.exists(default_path):
            with open(default_path, 'r') as f:
                return json.load(f)
        
        return {}
    
    def get_tracking_details(self, tracking_number: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Get mock tracking details for an AWB/tracking number."""
        _log_mock_call("get_tracking_details", f"tracking_number={tracking_number}")
        # Load mock tracking data
        mock_data = self._load_mock_data("shiprocket", "get_tracking", tracking_number)
        
        if mock_data and "tracking_data" in mock_data:
            tracking = mock_data["tracking_data"]
            activities = tracking.get("shipment_track_activities", [])
            latest = activities[0] if activities else {}
            
            return {
                "awb": tracking_number,
                "status": "Tracking available",
                "current_location": latest.get("location", "Mumbai Hub"),
                "latest_activity": latest.get("activity", "In Transit"),
                "last_update_date": latest.get("date", "2025-12-10 14:00:00"),
                "formatted_update": f"📍 Last update: {latest.get('activity', 'In Transit')} at {latest.get('location', 'Mumbai Hub')}"
            }
        
        # Default mock response
        return {
            "awb": tracking_number,
            "status": "Tracking available",
            "current_location": "Mock Hub - Bangalore",
            "latest_activity": "Out for Delivery",
            "last_update_date": "2025-12-10 10:00:00",
            "formatted_update": f"📍 Last update: Out for Delivery at Mock Hub - Bangalore"
        }
    
    def create_shipment(self, order_details: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a mock shipment."""
        _log_mock_call("create_shipment", f"order_details_keys={list(order_details.keys())}")
        return {
            "success": True,
            "shipment_id": "MOCK-SHIP-12345",
            "awb": "MOCK123456789",
            "courier": "Mock Express",
            "message": "Mock shipment created successfully"
        }
    
    def cancel_shipment(self, shipment_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Cancel a mock shipment."""
        _log_mock_call("cancel_shipment", f"shipment_id={shipment_id}")
        return {
            "success": True,
            "shipment_id": shipment_id,
            "message": "Mock shipment cancelled successfully"
        }
    
    def get_delivery_estimate(
        self,
        pickup_pincode: str,
        destination_pincode: str,
        weight: float = 0.5,
        cod: bool = False,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Get mock delivery estimate between two pincodes."""
        _log_mock_call("get_delivery_estimate", f"pickup={pickup_pincode}, dest={destination_pincode}")
        # Simulate different estimates based on pincodes
        if pickup_pincode == destination_pincode:
            estimated_days = "1 Day"
        elif pickup_pincode[:2] == destination_pincode[:2]:
            # Same state (approximate via first 2 digits)
            estimated_days = "2-3 Days"
        else:
            estimated_days = "4-6 Days"
        
        return {
            "status": "success",
            "origin_pincode": pickup_pincode,
            "destination_pincode": destination_pincode,
            "best_courier": "Mock Express",
            "estimated_delivery": estimated_days,
            "all_options_count": 5
        }
    
    def update_shipment_address(
        self,
        order_id: str,
        address_data: Dict[str, Any],
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Update shipping address for a mock shipment."""
        return {
            "success": True,
            "order_id": order_id,
            "message": "Mock address updated successfully",
            "updated_fields": list(address_data.keys())
        }
    
    def update_shipment_phone(
        self,
        order_id: str,
        new_phone: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Update phone number for a mock shipment."""
        return {
            "success": True,
            "order_id": order_id,
            "message": "Mock phone updated successfully",
            "new_phone": new_phone
        }
    
    def update_shipment_email(
        self,
        order_id: str,
        new_email: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Update email for a mock shipment."""
        return {
            "success": True,
            "order_id": order_id,
            "message": "Mock email updated successfully",
            "new_email": new_email
        }
    
    def update_shipment_name(
        self,
        order_id: str,
        first_name: str,
        last_name: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Update customer name for a mock shipment."""
        _log_mock_call("update_shipment_name", f"order_id={order_id}, name={first_name} {last_name}")
        return {
            "success": True,
            "order_id": order_id,
            "message": f"Mock name updated to {first_name} {last_name}",
            "new_name": f"{first_name} {last_name}"
        }
    
    def get_order_data(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Get mock order data from logistics system."""
        _log_mock_call("get_order_data", f"order_id={order_id}")
        # Load mock data
        mock_data = self._load_mock_data("shiprocket", "get_order_details", order_id)
        
        if mock_data and "order_data" in mock_data:
            return {
                "success": True,
                "found": True,
                "order_id": order_id,
                "logistics_order_id": mock_data.get("logistics_order_id", 12345),
                "status": mock_data.get("status", "In Transit"),
                "order_data": mock_data["order_data"],
                "shipments": mock_data.get("shipments", {}),
                "delivered_on": mock_data.get("delivered_on")
            }
        
        # Default mock response
        return {
            "success": True,
            "found": True,
            "order_id": order_id,
            "logistics_order_id": 12345,
            "status": "In Transit",
            "order_data": {
                "order_id": order_id,
                "awb_code": "MOCK123456789",
                "courier": "Mock Express",
                "pickup_date": "2025-12-08",
                "edd": "2025-12-12"
            },
            "shipments": {
                "awb_code": "MOCK123456789",
                "courier": "Mock Express"
            },
            "delivered_on": None
        }

