# Tool Refactoring Guide

## Current Status

### ✅ Fully Refactored (2 tools)
1. **`get_order_status`** - Uses `OrderStatusOrchestrator.get_order_status()`
2. **`get_customer_orders_by_phone`** - Uses `CustomerOrderOrchestrator.get_customer_orders()`
3. **`get_product_details_from_url_graphql`** - Uses `ShopifyProductAdapter` but not fully orchestrated

### ❌ Not Yet Refactored (19 tools)

#### Order Status Tools (2)
4. `get_order_status_summary` - Should delegate to `OrderStatusOrchestrator.get_order_status_summary()`
5. `get_delivery_timeline` - Should delegate to `OrderStatusOrchestrator.get_delivery_timeline()`

#### Order Details Tools (1)
6. `get_order_details_with_pricing` - Should use adapter pattern

#### Product Tools (1)
7. `get_product_availability` - Should use `ProductInterface`

#### Cancellation Tools (2)
8. `cancel_order_shopify` - Should use `CancellationOrchestrator`
9. `cancel_order_shiprocket` - Should use `CancellationOrchestrator`

#### Order Update Tools (7)
10. `update_shopify_order_tool`
11. `update_order_shiprocket`
12. `update_order_notes_shopify`
13. `update_order_tags_shopify`
14. `update_order_phone_number`
15. `update_order_email`
16. `update_order_email` (duplicate?)

#### Logistics Tools (3)
17. `get_shipment_current_location`
18. `get_delivery_estimate_by_pincode`

#### Business Logic (5) - Already Generic ✅
19. `detect_frustration`
20. `get_discount_information_tool`
21. `get_sales_policy_tool`
22. `escalate_bulk_order_request_tool`
23. `get_repeated_discount_message_tool`

## Refactoring Pattern

### Pattern 1: Simple Delegation (Used for get_order_status)

**Before:**
```python
@mcp.tool()
def get_order_status(order_id: str, state: dict = None) -> dict:
    # 200+ lines of hardcoded Shiprocket/Shopify logic
    token = get_shiprocket_token(state)
    orders = get_shiprocket_order_details(order_id, token)
    # ... lots of processing ...
    return result
```

**After:**
```python
@mcp.tool()
def get_order_status(order_id: str, state: dict = None) -> dict:
    """Fetch order info by order ID using configured orchestration strategy."""
    log_with_trace_id(state, f"Getting order status for {order_id}")
    
    try:
        from fashion_bot.core.orchestrator import OrderStatusOrchestrator
        result = OrderStatusOrchestrator.get_order_status(order_id, state=state)
        
        if result.get("total_orders_found", 0) == 0:
             raise ToolError(f"Order {order_id} not found.")
        
        return result
        
    except ToolError:
        raise
    except Exception as e:
        log_with_trace_id(state, f"Error: {e}", "error")
        raise ToolError(str(e))
```

**Result:**
- Tool code: 200+ lines → 15 lines (92% reduction)
- All vendor logic moved to Orchestrator
- Configuration-driven via `VendorConfigManager`

### Pattern 2: Adapter + Helper (Used for get_order_details_with_pricing)

**Before:**
```python
@mcp.tool()
def get_order_details_with_pricing(order_id: str, state: dict = None) -> dict:
    order = get_shopify_order(order_id, state)  # Hardcoded Shopify
    # ... 150 lines of processing ...
    return result
```

**After:**
```python
@mcp.tool()
def get_order_details_with_pricing(order_id: str, state: dict = None) -> dict:
    """Get comprehensive order details including pricing breakdown."""
    try:
        from fashion_bot.core.factory import ServiceFactory
        
        # Get primary vendor service (config-driven)
        primary_vendor = ServiceFactory.get_primary_vendor(state)
        order_service = ServiceFactory.get_order_service(state=state, vendor=primary_vendor)
        
        # Fetch order
        order = order_service.get_order_details(order_id, state=state)
        if not order:
            raise ToolError(f"Order {order_id} not found")
        
        # Process pricing details (helper function)
        return _process_order_pricing_details(order, state)
        
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(str(e))

def _process_order_pricing_details(order: dict, state: dict = None) -> dict:
    """Extract pricing details from order data (vendor-agnostic)."""
    # Processing logic here (vendor-agnostic)
    return result
```

**Result:**
- Fetching: Config-driven via `ServiceFactory`
- Processing: Vendor-agnostic helper function
- Tool: Thin coordinator

## Step-by-Step Refactoring Process

### For Order Status Tools (get_order_status_summary, get_delivery_timeline)

#### Step 1: Add Method to Orchestrator
```python
# fashion_bot/core/orchestrator.py

class OrderStatusOrchestrator:
    # ... existing methods ...
    
    @staticmethod
    def get_order_status_summary(order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get order status with detailed summary format.
        Reuses get_order_status but formats output differently.
        """
        # Reuse existing pipeline
        base_result = OrderStatusOrchestrator.get_order_status(order_id, state)
        
        if base_result.get("total_orders_found", 0) == 0:
            return base_result
        
        # Convert DTOs to summary format
        summary_orders = []
        for order in base_result.get("orders", []):
            summary_dto = create_order_status_summary_dto(
                order_id=order.get("order_id"),
                channel_order_id=order.get("channel_order_id"),
                # ... map all fields ...
            )
            summary_orders.append(summary_dto)
        
        return {
            "total_orders_found": len(summary_orders),
            "orders": summary_orders
        }
```

#### Step 2: Update Tool to Delegate
```python
# fashion_bot/tools.py

@mcp.tool()
def get_order_status_summary(order_id: str, state: dict = None) -> dict:
    """Get detailed order status summary."""
    try:
        from fashion_bot.core.orchestrator import OrderStatusOrchestrator
        result = OrderStatusOrchestrator.get_order_status_summary(order_id, state=state)
        
        if result.get("total_orders_found", 0) == 0:
             raise ToolError(f"Order {order_id} not found.")
        
        return result
        
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(str(e))
```

#### Step 3: Rename Old Implementation (for safety)
```python
# fashion_bot/tools.py

@mcp.tool()
def get_order_status_summary_OLD(order_id: str, state: dict = None) -> dict:
    """DEPRECATED: Old implementation kept for reference."""
    # ... old code ...
```

### For Product Tools (get_product_availability)

#### Step 1: Ensure Adapter Has Method
```python
# fashion_bot/shopify/tools/product_adapter.py

class ShopifyProductAdapter(ProductInterface):
    def check_product_availability(self, product_ref: str, size: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Check if a size is available for a product."""
        # Implementation here
        pass
```

#### Step 2: Update Tool
```python
# fashion_bot/tools.py

@mcp.tool()
def get_product_availability(product_ref: str, size: str, state: dict = None) -> dict:
    """Check if a particular size is available for a product."""
    try:
        from fashion_bot.core.factory import ServiceFactory
        
        product_service = ServiceFactory.get_product_service(state=state)
        result = product_service.check_product_availability(product_ref, size, state=state)
        
        if not result:
            raise ToolError(f"Could not check availability for {product_ref}")
        
        return result
        
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(str(e))
```

### For Cancellation Tools

#### Step 1: Use Existing CancellationOrchestrator
```python
# fashion_bot/core/orchestrator.py already has CancellationOrchestrator
```

#### Step 2: Update Tools
```python
# fashion_bot/tools.py

@mcp.tool()
def cancel_order_shopify(order_id: str, cancellation_reason: str, state: dict) -> dict:
    """Cancel order in Shopify."""
    try:
        from fashion_bot.core.orchestrator import CancellationOrchestrator
        result = CancellationOrchestrator.cancel_order(order_id, cancellation_reason, state=state)
        
        if result.get("order_cancellation", {}).get("status") == "error":
            raise ToolError(f"Cancellation failed: {result['order_cancellation']['error']}")
        
        return result
        
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(str(e))

@mcp.tool()
def cancel_order_shiprocket(order_id: str, state: dict) -> dict:
    """Cancel order in Shiprocket (logistics)."""
    try:
        from fashion_bot.core.factory import ServiceFactory
        
        logistics_service = ServiceFactory.get_logistics_service(state=state)
        result = logistics_service.cancel_shipment(order_id, state=state)
        
        return result
        
    except Exception as e:
        raise ToolError(str(e))
```

### For Order Update Tools

#### Step 1: Add Interface Method
```python
# fashion_bot/interfaces/order.py

class OrderInterface(ABC):
    # ... existing methods ...
    
    @abstractmethod
    def update_order_address(self, order_id: str, new_address: Dict, state: Optional[Dict] = None) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    def update_order_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    def update_order_notes(self, order_id: str, note: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    def update_order_tags(self, order_id: str, tags: List[str], state: Optional[Dict] = None) -> Dict[str, Any]:
        pass
```

#### Step 2: Implement in Adapter
```python
# fashion_bot/shopify/tools/order_adapter.py

class ShopifyOrderAdapter(OrderInterface):
    def update_order_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Update phone number for a Shopify order."""
        # Implementation
        pass
```

#### Step 3: Update Tool
```python
# fashion_bot/tools.py

@mcp.tool()
def update_order_phone_number(order_id: str, new_phone: str, state: dict) -> dict:
    """Update phone number for an order."""
    try:
        from fashion_bot.core.factory import ServiceFactory
        
        primary_vendor = ServiceFactory.get_primary_vendor(state)
        order_service = ServiceFactory.get_order_service(state=state, vendor=primary_vendor)
        
        result = order_service.update_order_phone(order_id, new_phone, state=state)
        return result
        
    except Exception as e:
        raise ToolError(str(e))
```

### For Logistics Tools

#### Step 1: Add Interface Method
```python
# fashion_bot/interfaces/logistics.py

class LogisticsInterface(ABC):
    @abstractmethod
    def get_shipment_location(self, awb: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    def get_delivery_estimate(self, pickup_pincode: str, destination_pincode: str, 
                             weight: float, cod: bool, state: Optional[Dict] = None) -> Dict[str, Any]:
        pass
```

#### Step 2: Implement in Adapter
```python
# fashion_bot/shiprocket/tools/logistics_adapter.py

class ShiprocketLogisticsAdapter(LogisticsInterface):
    def get_shipment_location(self, awb: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Get current location of shipment."""
        # Implementation
        pass
```

#### Step 3: Update Tool
```python
# fashion_bot/tools.py

@mcp.tool()
def get_shipment_current_location(awb: str, state: dict = None) -> dict:
    """Get the current location and latest tracking update for a shipment."""
    try:
        from fashion_bot.core.factory import ServiceFactory
        
        logistics_service = ServiceFactory.get_logistics_service(state=state)
        result = logistics_service.get_shipment_location(awb, state=state)
        
        return result
        
    except Exception as e:
        raise ToolError(str(e))
```

## Summary

### Current Progress
- **Total Tools**: 22
- **Refactored**: 2 (9%)
- **Remaining**: 20 (91%)

### Refactoring Benefits
- **Code Reduction**: 200+ lines → 15 lines per tool (avg 92% reduction)
- **Vendor Decoupling**: Zero hardcoded vendor names in tools
- **Configuration**: All vendor selection driven by `VendorConfigManager`
- **Maintainability**: Single place to change vendor logic
- **Testability**: Easy to mock services for testing

### Estimated Effort
- **Order Status Tools** (2): 2 hours
- **Product Tools** (1): 1 hour
- **Cancellation Tools** (2): 1 hour
- **Order Update Tools** (7): 4 hours
- **Logistics Tools** (3): 2 hours
- **Total**: ~10 hours

### Next Steps
1. Complete order status tools refactoring
2. Add update methods to `OrderInterface`
3. Implement update methods in `ShopifyOrderAdapter`
4. Refactor all update tools
5. Add logistics methods to `LogisticsInterface`
6. Refactor logistics tools
7. Test all refactored tools
8. Remove `_OLD` deprecated functions

The architecture is ready—it's just systematic migration of each tool!

