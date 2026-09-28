from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List

class OrderInterface(ABC):
    """Interface for Order related operations"""
    
    @abstractmethod
    def get_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Get comprehensive order details"""
        pass

    @abstractmethod
    def get_orders_by_customer_phone(self, phone: str, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Get orders for a specific customer phone"""
        pass
        
    @abstractmethod
    def create_order(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a new order"""
        pass
        
    @abstractmethod
    def cancel_order(
        self,
        order_id: str,
        reason: str = "",
        state: Optional[Dict] = None,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        """Cancel an order"""
        pass
    
    @abstractmethod
    def update_order(self, order_id: str, update_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Update an order (e.g. address, notes)"""
        pass

    @abstractmethod
    def filter_delivered_orders(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter orders to only include those with 'Delivered' status"""
        pass

    @abstractmethod
    def format_delivered_orders_for_display(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Format delivered orders for display to the user"""
        pass

    @abstractmethod
    def build_delivered_orders_response(self, orders: List[Dict[str, Any]], phone_number: str) -> Dict[str, Any]:
        """Build a complete response for delivered orders query"""
        pass

    def get_customer_by_phone(self, phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get customer details by phone number.
        
        Args:
            phone: Customer phone number
            state: Optional state dictionary
            
        Returns:
            Dictionary with customer data or not found status
        """
        # Default implementation - can be overridden by vendors
        return {"found": False, "message": "Customer lookup not implemented"}

    async def aget_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Async variant of get_order_details."""
        return self.get_order_details(order_id, state=state)

    async def aget_orders_by_customer_phone(
        self,
        phone: str,
        limit: int = 5,
        state: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """Async variant of get_orders_by_customer_phone."""
        return self.get_orders_by_customer_phone(phone, limit=limit, state=state)

    async def acreate_order(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Async variant of create_order."""
        return self.create_order(order_data, state=state)

    async def acreate_order_multi(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a single order containing multiple line items.

        Default implementation is unsupported; vendors that support multi
        line-item orders (e.g. Shopify) override this.
        """
        return {"success": False, "error": "Multi-item order creation not supported for this vendor"}

    async def acancel_order(
        self,
        order_id: str,
        reason: str = "",
        state: Optional[Dict] = None,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        """Async variant of cancel_order."""
        return self.cancel_order(
            order_id,
            reason=reason,
            state=state,
            custom_note=custom_note,
            skip_refund=skip_refund,
        )

    async def aupdate_order(
        self,
        order_id: str,
        update_data: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of update_order."""
        return self.update_order(order_id, update_data, state=state)

    async def aget_customer_by_phone(self, phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Async variant of get_customer_by_phone."""
        return self.get_customer_by_phone(phone, state=state)

    def add_order_note(self, order_id: str, note: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Add a note to an order.
        
        Args:
            order_id: Order ID or name
            note: Note text to add
            state: Optional state dictionary
            
        Returns:
            Dictionary with result
        """
        # Default implementation - can be overridden by vendors
        return {"success": False, "message": "Add note not implemented"}

    async def aadd_order_note(
        self,
        order_id: str,
        note: str,
        state: Optional[Dict] = None,
        order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of add_order_note.

        ``order_record`` is an optional, already-fetched raw order record a
        caller may supply so an adapter can skip a redundant lookup. Adapters
        that cannot use it should ignore it.
        """
        return self.add_order_note(order_id, note, state=state)

    def add_order_tags(self, order_id: str, tags: List[str], state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Add tags to an order.
        
        Args:
            order_id: Order ID or name
            tags: List of tags to add
            state: Optional state dictionary
            
        Returns:
            Dictionary with result
        """
        # Default implementation - can be overridden by vendors
        return {"success": False, "message": "Add tags not implemented"}

    async def aadd_order_tags(
        self,
        order_id: str,
        tags: List[str],
        state: Optional[Dict] = None,
        order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of add_order_tags.

        ``order_record`` is an optional, already-fetched raw order record a
        caller may supply so an adapter can skip a redundant lookup. Adapters
        that cannot use it should ignore it.
        """
        return self.add_order_tags(order_id, tags, state=state)

    def update_order_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Update phone number for an order.
        
        Args:
            order_id: Order ID or name
            new_phone: New phone number
            state: Optional state dictionary
            
        Returns:
            Dictionary with result
        """
        # Default implementation - can be overridden by vendors
        return {"success": False, "message": "Update phone not implemented"}

    async def aupdate_order_phone(
        self,
        order_id: str,
        new_phone: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of update_order_phone."""
        return self.update_order_phone(order_id, new_phone, state=state)

    def update_order_email(self, order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Update email address for an order.
        
        Args:
            order_id: Order ID or name
            new_email: New email address
            state: Optional state dictionary
            
        Returns:
            Dictionary with result
        """
        # Default implementation - can be overridden by vendors
        return {"success": False, "message": "Update email not implemented"}

    async def aupdate_order_email(
        self,
        order_id: str,
        new_email: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of update_order_email."""
        return self.update_order_email(order_id, new_email, state=state)

    def clone_order(
        self,
        original_order_data: Dict[str, Any],
        new_line_items: List[Dict[str, Any]],
        state: Optional[Dict] = None,
        note: str = "",
        additional_tags: Optional[List[str]] = None,
        financial_status_override: Optional[str] = None,
        payment_gateway_names_override: Optional[List[str]] = None,
        transactions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Clone an existing order with modified line items.

        Preserves the original order's shipping/billing address, customer,
        email, phone, discount codes, note attributes, tax settings, and tags.
        Only the line items (and optionally financial fields) are replaced.

        Args:
            original_order_data: Full order dict from aget_order_details
            new_line_items: Replacement line items, each with at minimum
                ``variant_id`` and ``quantity``
            state: Optional state dictionary
            note: Order note for the cloned order
            additional_tags: Extra tags to append (original tags are preserved)
            financial_status_override: If set, overrides the original financial_status
            payment_gateway_names_override: If set, overrides the original gateways
            transactions: If set, attaches these transactions to the new order

        Returns:
            Dict with success, order (Shopify order object), order_id, etc.
        """
        return {"success": False, "message": "Clone order not implemented"}

    async def aclone_order(
        self,
        original_order_data: Dict[str, Any],
        new_line_items: List[Dict[str, Any]],
        state: Optional[Dict] = None,
        note: str = "",
        additional_tags: Optional[List[str]] = None,
        financial_status_override: Optional[str] = None,
        payment_gateway_names_override: Optional[List[str]] = None,
        transactions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Async variant of clone_order."""
        return self.clone_order(
            original_order_data,
            new_line_items,
            state=state,
            note=note,
            additional_tags=additional_tags,
            financial_status_override=financial_status_override,
            payment_gateway_names_override=payment_gateway_names_override,
            transactions=transactions,
        )

    def refund_order(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Issue a full refund for a cancelled order.

        The implementation must calculate the refund amounts and create the
        refund transaction against the original payment method.

        Args:
            order_id: Order ID or name
            state: Optional state dictionary

        Returns:
            Dictionary with success status, refund_id, and amount
        """
        return {"success": False, "message": "Refund not implemented"}

    async def arefund_order(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant of refund_order."""
        return self.refund_order(order_id, state=state)
