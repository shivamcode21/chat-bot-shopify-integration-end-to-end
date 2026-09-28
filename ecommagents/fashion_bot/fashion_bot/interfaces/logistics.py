from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List

class LogisticsInterface(ABC):
    """Interface for Logistics/Shipping related operations"""
    
    @abstractmethod
    def get_tracking_details(self, tracking_number: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Get tracking status"""
        pass

    @abstractmethod
    def create_shipment(self, order_details: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a shipment"""
        pass
        
    @abstractmethod
    def cancel_shipment(self, shipment_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Cancel a shipment"""
        pass

    def update_shipment_address(self, order_id: str, address_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Update shipping address for a shipment.
        
        Args:
            order_id: Order ID
            address_data: Dictionary with address fields (name, address1, address2, city, state, zip, phone, email)
            state: Optional state dictionary
            
        Returns:
            Dictionary with update result
        """
        # Default implementation - can be overridden by vendors
        return {"success": False, "message": "Update address not implemented"}

    async def aget_tracking_details(
        self,
        tracking_number: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of get_tracking_details."""
        return self.get_tracking_details(tracking_number, state=state)

    async def acreate_shipment(
        self,
        order_details: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of create_shipment."""
        return self.create_shipment(order_details, state=state)

    async def acancel_shipment(
        self,
        shipment_id: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of cancel_shipment."""
        return self.cancel_shipment(shipment_id, state=state)

    async def aupdate_shipment_address(
        self,
        order_id: str,
        address_data: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of update_shipment_address."""
        return self.update_shipment_address(order_id, address_data, state=state)

    def update_shipment_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Update phone number for a shipment.
        
        Args:
            order_id: Order ID
            new_phone: New phone number
            state: Optional state dictionary
            
        Returns:
            Dictionary with update result
        """
        # Default implementation - can be overridden by vendors
        return {"success": False, "message": "Update phone not implemented"}

    async def aupdate_shipment_phone(
        self,
        order_id: str,
        new_phone: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of update_shipment_phone."""
        return self.update_shipment_phone(order_id, new_phone, state=state)

    def update_shipment_email(self, order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Update email for a shipment.
        
        Args:
            order_id: Order ID
            new_email: New email address
            state: Optional state dictionary
            
        Returns:
            Dictionary with update result
        """
        # Default implementation - can be overridden by vendors
        return {"success": False, "message": "Update email not implemented"}

    async def aupdate_shipment_email(
        self,
        order_id: str,
        new_email: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of update_shipment_email."""
        return self.update_shipment_email(order_id, new_email, state=state)

    def update_shipment_name(self, order_id: str, first_name: str, last_name: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Update customer name for a shipment.
        
        Args:
            order_id: Order ID
            first_name: New first name
            last_name: New last name
            state: Optional state dictionary
            
        Returns:
            Dictionary with update result
        """
        # Default implementation - can be overridden by vendors
        return {"success": False, "message": "Update name not implemented"}

    async def aupdate_shipment_name(
        self,
        order_id: str,
        first_name: str,
        last_name: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of update_shipment_name."""
        return self.update_shipment_name(order_id, first_name, last_name, state=state)

    def get_order_data(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get complete order data from logistics system.
        
        Args:
            order_id: Order ID or name
            state: Optional state dictionary
            
        Returns:
            Dictionary with order data including status, delivery date, shipment info
        """
        # Default implementation - can be overridden by vendors
        return {"success": False, "message": "Get order data not implemented"}

    async def aget_order_data(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Async variant of get_order_data."""
        return self.get_order_data(order_id, state=state)

