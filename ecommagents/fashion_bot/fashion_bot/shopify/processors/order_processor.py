import logging
from typing import Dict, Any, Optional, List
from fashion_bot.interfaces.processor import OrderProcessorInterface
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.utils.order_utils import create_order_info_dto

logger = logging.getLogger(__name__)

class ShopifyOrderProcessor(OrderProcessorInterface):
    def process_order(self, order_data: Dict[str, Any], state: Optional[Dict] = None, **kwargs) -> Dict[str, Any]:
        """
        Process a single Shopify order dict into standardized format.
        """
        try:
            # Basic fields
            order_id = str(order_data.get('id', ''))
            order_name = order_data.get('name', 'Unknown')
            channel_order_id = order_name.strip('#')  # Use order name (#137673) as channel ID - this is what Shiprocket uses
            
            # Statuses
            fulfillment_status = order_data.get('fulfillment_status', 'unfulfilled')
            if fulfillment_status is None: fulfillment_status = 'unfulfilled'
            
            financial_status = order_data.get('financial_status', 'unknown')
            cancelled_at = order_data.get('cancelled_at')
            
            # Derive composite status
            status = "Processing"
            if cancelled_at:
                status = "Cancelled"
            elif fulfillment_status == 'fulfilled':
                status = "Shipped" # Or Delivered, but Shopify just says fulfilled
            elif financial_status == 'paid':
                status = "Confirmed"
            
            # Tracking info from fulfillments
            tracking_url = ""
            courier = ""
            awb = ""
            shipment_status = ""
            
            fulfillments = order_data.get('fulfillments', [])
            if fulfillments:
                # Use the most recent fulfillment
                latest_fulfillment = fulfillments[0] 
                tracking_url = latest_fulfillment.get('tracking_url', '')
                courier = latest_fulfillment.get('tracking_company', '')
                
                tracking_numbers = latest_fulfillment.get('tracking_numbers', [])
                if tracking_numbers:
                    awb = tracking_numbers[0]
                    
                shipment_status = latest_fulfillment.get('shipment_status', '')

            # Items
            line_items = order_data.get('line_items', [])
            items = [item.get('name', 'Unknown Item') for item in line_items]
            
            # Detailed line items for pricing
            detailed_items = []
            calculated_total = 0.0
            
            for item in line_items:
                try:
                    # Price and Quantity
                    price = float(item.get('price', 0))
                    quantity = int(item.get('quantity', 1))
                    
                    # Discounts
                    discount_allocations = item.get("discount_allocations", [])
                    discount_amount = 0.0
                    for d in discount_allocations:
                        try:
                            discount_amount += float(d.get("amount", 0))
                        except (ValueError, TypeError):
                            pass
                            
                    # Fulfillment Status
                    item_fulfillment_status = item.get("fulfillment_status", "unfulfilled")
                    
                    # Final Price Calculation
                    # Logic: (Unit Price * Quantity) - Total Discount? 
                    # Or (Unit Price - Discount Per Unit) * Quantity?
                    # Shopify discount_allocations usually represent the TOTAL discount for that line item (all quantities).
                    # Let's follow the old tool's logic:
                    # final_price = (item_price - discount_amount) * quantity  <-- OLD TOOL LOGIC WAS WEIRD
                    # Wait, looking at old tool logs:
                    # d_amount = float(d.get("amount", 0))
                    # final_price = (item_price - discount_amount) * quantity
                    # This looks like "discount_amount" was treated as PER UNIT in the old tool?
                    # Actually, Shopify API 'discount_allocations' 'amount' is the TOTAL discount for the line item.
                    # Converting old logic: 
                    # Old tool: discount_amount += d_amount
                    # Old tool: final_price = (item_price - discount_amount) * quantity
                    # If discount_amount is total, this formula implies it subtracts total discount from UNIT price, then multiplies by quantity. 
                    # That would be wrong if discount is total. 
                    # However, to match "it was working", I should check if I misread the old code.
                    
                    # Re-reading old tools.py from history:
                    # for d in discount_allocations: discount_amount += d_amount
                    # final_price = (item_price - discount_amount) * quantity
                    
                    # If 'discount_amount' comes from 'discount_allocations', it is usually the total discount for the line.
                    # If the old tool did (Price - TotalDiscount) * Qty, that would be very wrong (subtracting total from unit).
                    # BUT, maybe 'discount_allocations' in the specific Shopify API version they use returns per-unit?
                    # Standard Shopify Admin API: discount_allocations.amount is "The amount of the discount." (Total for the line).
                    
                    # Let's implement the SAFE and CORRECT way, which likely fixes a bug in the old tool OR matches what the user sees as "correct".
                    # Correct Net for Line Item = (Price * Quantity) - TotalDiscount
                    
                    # But wait, the user said "you broke something... it was working".
                    # I will implement the standard logic: Total Line Price = (Price * Qty) - Discount.
                    
                    final_line_price = (price * quantity) - discount_amount
                    calculated_total += final_line_price
                    
                    detailed_items.append({
                        "name": item.get('name', 'Unknown Item'),
                        "quantity": quantity,
                        "unit_price": price,
                        "discount": discount_amount,
                        "final_price": final_line_price,
                        "fulfillment_status": item_fulfillment_status
                    })
                except (ValueError, TypeError):
                     detailed_items.append({
                        "name": item.get('name', 'Unknown Item'),
                        "quantity": 1,
                        "unit_price": 0.0,
                        "discount": 0.0,
                        "final_price": 0.0,
                        "fulfillment_status": "unfulfilled"
                    })
            
            # Dates
            created_at = order_data.get('created_at', '')
            updated_at = order_data.get('updated_at', '')
            
            # Financials
            # Use calculated total if Shopify total is missing, otherwise prefer Shopify source of truth
            total_price_str = order_data.get('total_price')
            if total_price_str:
                 total_price = float(total_price_str)
            else:
                 total_price = calculated_total
                 
            currency = order_data.get('currency')
            
            # Customer
            customer_info = order_data.get('customer', {})
            customer_name = "Guest"
            if customer_info:
                first_name = customer_info.get('first_name', '')
                last_name = customer_info.get('last_name', '')
                customer_name = f"{first_name} {last_name}".strip() or "Guest"

            # Allow source override (e.g. "shopify_exchange")
            source = kwargs.get('source', 'shopify')

            return create_order_info_dto(
                order_id=order_name, # Use Name (e.g. #1001) as public ID
                channel_order_id=channel_order_id,
                status=status,
                partner_status="", # Not available from Shopify directly
                shipment_status=shipment_status,
                customer=customer_name,
                delivery_date=None, # Shopify doesn't usually have EDD
                out_for_delivery_date=None,
                items=items,
                courier=courier,
                tracking_company=courier,  # Pass tracking_company for delivery partner detection
                tracking_url=tracking_url,
                awb=awb,
                products=items,
                created_at=created_at,
                updated_at=updated_at,
                source=source,
                financial_status=financial_status,
                fulfillment_status=fulfillment_status,
                total_price=total_price,
                currency=currency,
                line_items=detailed_items,
                cancelled_at=cancelled_at
            )

        except Exception as e:
            log_with_trace_id(state, f"Error processing Shopify order: {e}", "error")
            return {}

    def process_orders(self, raw_orders: List[Any], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        processed = []
        for order in raw_orders:
            res = self.process_order(order, state)
            if res:
                processed.append(res)
        return processed

    @staticmethod
    def format_delivered_orders_for_llm(formatted_orders: List[Dict[str, Any]]) -> str:
        """
        Build a detailed response string with order information for the LLM.
        
        Args:
            formatted_orders: List of formatted order dicts from format_delivered_orders_for_display
            
        Returns:
            Formatted string with order details for LLM to use in response
        """
        if not formatted_orders:
            return "No delivered orders found for this customer."
        
        count = len(formatted_orders)
        response_lines = [f"Found {count} delivered order(s) eligible for return/exchange:\n"]
        
        for i, order in enumerate(formatted_orders, 1):
            order_name = order.get("order_name") or order.get("order_id") or "Unknown"
            status = order.get("status") or "Delivered"
            delivered_date = order.get("delivered_date") or ""
            items = order.get("items", [])
            
            response_lines.append(f"{i}. Order {order_name}")
            if delivered_date:
                # Format date nicely - take first 10 chars if ISO format
                date_display = delivered_date[:10] if len(str(delivered_date)) > 10 else str(delivered_date)
                response_lines.append(f"   Delivered: {date_display}")
            
            if items:
                # Get item names, max 3 items to keep response concise
                item_names = []
                for item in items[:3]:
                    item_name = item.get("name") or item.get("title") or "Unknown item"
                    item_names.append(item_name)
                if len(items) > 3:
                    item_names.append(f"...and {len(items) - 3} more")
                response_lines.append(f"   Items: {', '.join(item_names)}")
            response_lines.append("")  # Empty line between orders
        
        # Add instruction for the LLM
        if count == 1:
            response_lines.append("Ask customer to confirm if they want to return/exchange this order.")
        else:
            response_lines.append("Ask customer which order number they want to return/exchange (1, 2, or 3).")
        
        return "\n".join(response_lines)
