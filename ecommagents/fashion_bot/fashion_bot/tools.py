import re
from datetime import datetime
from typing import List, Dict, Optional, Any
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
import json
from fashion_bot.config_manager import aget_config
from fashion_bot.utils.utils import log_with_trace_id

# Import helper functions from tool_helpers module
from fashion_bot.tool_helpers import (
    _get_client_id_from_state,
    get_order_prefix,
    get_allowed_urls,
    get_allowed_domains,
    is_url_allowed,
    normalize_order_id_variants,
    classify_status,
    create_order_info_dto,
    create_order_status_summary_dto,
    create_delivery_timeline_dto,
    format_etd_date,
    validate_phone_number_access,
    validate_indian_pincode,
)

# Initialize FastMCP server instance
mcp = FastMCP("FashionSupport")

# Multi-client support: Import shared context
from fashion_bot.client_context import get_client_id


# ==================== CUSTOMER ORDER TOOLS ====================

@mcp.tool()
async def get_customer_orders_by_phone(phone: str, state: dict = None) -> dict:
    """Fetch all orders for a customer by phone number using configured orchestration strategy."""
    log_with_trace_id(state, f"Getting customer orders for phone: {phone}")

    try:
        from fashion_bot.core.orchestrator import CustomerOrderOrchestrator
        result = await CustomerOrderOrchestrator.aget_customer_orders(phone, state=state)

        if result.get("total_orders", 0) == 0:
             log_with_trace_id(state, f"No orders found for phone {phone}", "warning")
        raise ToolError(f"No customer found with phone number {phone}")

        log_with_trace_id(state, f"Customer orders result: {result.get('total_orders')} orders")
        return result

    except ToolError:
        raise
    except Exception as e:
        log_with_trace_id(state, f"Error getting customer orders: {e}", "error")
        raise ToolError(str(e))


# ==================== ORDER CANCELLATION & UPDATE TOOLS ====================

@mcp.tool()
async def update_order_shiprocket(order_id: str, shipping_address: dict, state: dict) -> dict:
    """
    Update shipping address for an existing order. Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import OrderUpdateOrchestrator
    try:
        # Format address into pipe-separated string for OrderUpdateOrchestrator
        name = f"{shipping_address.get('first_name', '')} {shipping_address.get('last_name', '')}".strip()
        address_str = "|".join([
            name,
            shipping_address.get('address1', ''),
            shipping_address.get('address2', ''),
            shipping_address.get('city', ''),
            shipping_address.get('state', ''),
            shipping_address.get('zip', ''),
            shipping_address.get('phone', '')
        ])

        result = await OrderUpdateOrchestrator.aupdate_order(
            order_id=order_id,
            update_type='address',
            new_address=address_str,
            state=state
        )

        if result.get("success"):
            return {
                "success": True,
                "message": f"Order {order_id} shipping address updated successfully",
                "order_data": result
            }
        else:
            return {
                "success": False,
                "error": result.get("error", "Unknown error updating order"),
                "details": result
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to update order: {str(e)}"
        }


@mcp.tool()
async def update_order_notes_shopify(order_id: str, note: str, state: dict) -> dict:
    """
    Add a note to an existing order. Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import OrderUpdateOrchestrator
    try:
        result = await OrderUpdateOrchestrator.aupdate_notes(
            order_id=order_id,
            notes=note,
            state=state
        )

        if result.get("success"):
            return {
                "success": True,
                "message": f"Note added to order {order_id} successfully",
                "note_added": result.get("note_added", note)
            }
        else:
            return {
                "success": False,
                "error": result.get("error", "Unknown error adding note")
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to add note: {str(e)}"
        }


@mcp.tool()
async def update_order_tags_shopify(order_id: str, tags: list, state: dict) -> dict:
    """
    Add tags to an existing order. Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import OrderUpdateOrchestrator
    try:
        result = await OrderUpdateOrchestrator.aupdate_tags(
            order_id=order_id,
            tags=tags,
            state=state
        )

        if result.get("success"):
            return {
                "success": True,
                "message": f"Tags added to order {order_id} successfully",
                "tags_added": result.get("tags_added", tags)
            }
        else:
            return {
                "success": False,
                "error": result.get("error", "Unknown error adding tags")
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to add tags: {str(e)}"
        }


@mcp.tool()
async def cancel_order_shopify(order_id: str, cancellation_reason: str, state: dict) -> dict:
    """
    Cancel an existing order in the order management system. Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import CancellationOrchestrator
    try:
        result = await CancellationOrchestrator.acancel_order(
            order_id=order_id,
            reason=cancellation_reason,
            state=state
        )

        if result.get("success"):
            return {
                "success": True,
                "message": result.get("message", f"Order {order_id} has been cancelled"),
                "cancelled": True,
                "cancellation_reason": cancellation_reason
            }
        else:
            return {
                "success": False,
                "error": result.get("error", "Unknown error")
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to cancel order: {str(e)}"
        }


@mcp.tool()
async def cancel_order_shiprocket(order_id: str, state: dict) -> dict:
    """
    Cancel a shipment in the logistics/delivery system. Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import CancellationOrchestrator
    try:
        result = await CancellationOrchestrator.acancel_shipment(
            order_id=order_id,
            state=state
        )

        if result.get("success"):
            return {
                "success": True,
                "message": result.get("message", f"Shipment for order {order_id} has been cancelled")
            }
        else:
            # If order not found in logistics, it may not have been shipped yet
            error = result.get("error", "Unknown error")
            if "not found" in error.lower():
                return {
                    "success": True,
                    "message": f"Order {order_id} not found in logistics system (may not have been shipped yet)",
                    "warning": "Order may already be cancelled or not yet created in logistics system"
                }

            return {
                "success": False,
                "error": error
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to cancel shipment: {str(e)}"
        }


@mcp.tool()
async def update_order_phone_number(order_id: str, new_phone: str, state: dict) -> dict:
    """
    Update customer phone number for an existing order. Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import OrderUpdateOrchestrator
    try:
        result = await OrderUpdateOrchestrator.aupdate_phone(
            order_id=order_id,
            new_phone=new_phone,
            state=state
        )
        return result
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to update phone number: {str(e)}"
        }


@mcp.tool()
async def update_order_email(order_id: str, new_email: str, state: dict) -> dict:
    """
    Update customer email address for an existing order. Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import OrderUpdateOrchestrator
    try:
        result = await OrderUpdateOrchestrator.aupdate_email(
            order_id=order_id,
            new_email=new_email,
            state=state
        )
        return result
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to update email: {str(e)}"
        }


# ==================== VALIDATION & UTILITY TOOLS ====================

@mcp.tool()
def validate_pincode_tool(pincode: str, state: dict = None) -> dict:
    """
    Validate if a pincode/postal code is valid (accepts any country format). Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import UtilityOrchestrator
    try:
        result = UtilityOrchestrator.validate_pincode(pincode=pincode, state=state)
        return result
    except Exception as e:
        return {
            "valid": False,
            "error": str(e),
            "message": "Error validating pincode"
        }


@mcp.tool()
def is_url_in_valid_domain_tool(url: str, state: dict = None) -> dict:
    """
    Check if a URL belongs to a valid/supported domain. Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import UtilityOrchestrator
    try:
        result = UtilityOrchestrator.validate_url_domain(url=url, state=state)
        return result
    except Exception as e:
        return {
            "valid": False,
            "error": str(e),
            "message": "Error validating URL domain"
        }


@mcp.tool()
def get_category_links_tool(state: dict = None) -> dict:
    """
    Fetch category links/URLs from database configuration. Vendor/product agnostic.
    """
    from fashion_bot.core.orchestrator import UtilityOrchestrator
    try:
        result = UtilityOrchestrator.get_available_categories(state=state)
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Error fetching category links"
        }


# ==================== PRODUCT TOOLS ====================

@mcp.tool()
def check_product_availability_tool(product_ref: str, size: str, state: dict = None) -> dict:
    """Check if a specific size is available for a product. Vendor/product agnostic."""
    from fashion_bot.core.orchestrator import ProductOrchestrator
    try:
        result = ProductOrchestrator.check_availability(
            product_ref=product_ref,
            size=size,
            state=state
        )
        return result
    except Exception as e:
        return {
            "available": False,
            "error": str(e),
            "product": product_ref,
            "size": size
        }


# ==================== DELIVERY TOOLS ====================

@mcp.tool()
async def get_delivery_estimate_tool_enhanced(pickup_pincode: str, destination_pincode: str, weight: float = 0.5, cod: bool = False, state: dict = None) -> dict:
    """Get delivery estimate between two pincodes. Vendor/product agnostic."""
    from fashion_bot.core.orchestrator import LogisticsOrchestrator
    try:
        result = await LogisticsOrchestrator.get_delivery_estimate(
            pickup_pincode=pickup_pincode,
            destination_pincode=destination_pincode,
            weight=weight,
            cod=cod,
            state=state
        )
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "pickup_pincode": pickup_pincode,
            "destination_pincode": destination_pincode
        }


# ==================== CATEGORY TOOLS ====================

@mcp.tool()
async def get_category_urls_tool(state: dict = None) -> dict:
    """
    Get all available category URLs from database.
    Returns category names mapped to their collection URLs.

    Args:
        state: Optional state dict containing client_id

    Returns:
        Dictionary with success status and category data
    """
    try:
        from fashion_bot.config_manager import aget_json_config
        from fashion_bot.client_context import get_client_id
        from fashion_bot.utils.utils import log_with_trace_id

        log_with_trace_id(state, "Fetching category URLs from database")

        # Get client_id from context or state
        client_id = state.get("client_id") if state else None
        if not client_id:
            client_id = get_client_id()

        # Fetch category URLs from database
        category_data = await aget_json_config("category_urls", client_id)

        if category_data:
            log_with_trace_id(state, f"Found {len(category_data)} categories")
            return {
                "success": True,
                "categories": category_data,
                "message": f"Retrieved {len(category_data)} categories from database"
            }
        else:
            log_with_trace_id(state, "No category data found in database")
            return {
                "success": False,
                "error": "No categories configured",
                "message": "Category information is not available"
            }

    except Exception as e:
        log_with_trace_id(state, f"Error fetching category URLs: {str(e)}", "error")
        return {
            "success": False,
            "error": str(e),
            "message": "Error retrieving category information"
        }


# ==================== MESSAGE FORMATTING TOOLS ====================

@mcp.tool()
def format_message_for_whatsapp_tool(message: str, state: dict = None) -> str:
    """Format a message for WhatsApp. Vendor/product agnostic."""
    import re
    try:
        # Simple WhatsApp formatting - just return the message with basic cleanup
        # Remove any HTML tags
        formatted = re.sub(r'<[^>]+>', '', message)
        # Normalize whitespace but preserve newlines
        # Replace multiple spaces/tabs with single space (but not newlines)
        formatted = re.sub(r'[^\S\n]+', ' ', formatted)
        # Replace multiple newlines with double newlines
        formatted = re.sub(r'\n{3,}', '\n\n', formatted)
        # Clean up any trailing/leading whitespace on each line
        formatted = '\n'.join(line.strip() for line in formatted.split('\n'))
        return formatted.strip()
    except Exception as e:
        return message


@mcp.tool()
def clean_markdown_links_tool(message: str, state: dict = None) -> str:
    """Remove markdown link formatting from a message. Vendor/product agnostic."""
    import re
    try:
        # Convert markdown links [text](url) to just url
        cleaned = re.sub(r'\[([^\]]*)\]\s*\(([^)]+)\)', r'\2', message)
        return cleaned
    except Exception as e:
        return message


@mcp.tool()
async def replace_urls_in_message_tool(message: str, shopify_url: str = None, website_url: str = None, state: dict = None) -> str:
    """Replace Shopify URLs with production website URLs. Vendor/product agnostic.

    Args:
        message: The message containing URLs to replace
        shopify_url: Optional - Shopify URL to replace. If not provided, fetched from config.
        website_url: Optional - Production website URL to use. If not provided, fetched from config.
        state: Optional state dict for client context

    Returns:
        Message with Shopify URLs replaced with production website URLs
    """
    import re
    from fashion_bot.config_manager import aget_config

    try:
        # If URLs not provided, fetch from config
        if not shopify_url or not website_url:
            client_id = state.get("client_id") if state else None
            website_config = await aget_config('website_urls', client_id=client_id)

            if isinstance(website_config, dict):
                shopify_url = shopify_url or website_config.get('shopify_url')
                website_url = website_url or website_config.get('website_url')

        # Still no URLs? Return message unchanged
        if not shopify_url or not website_url:
            return message

        # Clean up the URLs - remove protocol prefix for comparison
        shopify_domain = shopify_url.replace('https://', '').replace('http://', '').rstrip('/')
        website_domain = website_url.replace('https://', '').replace('http://', '').rstrip('/')

        if not shopify_domain or not website_domain:
            return message

        # Replace all variations of the Shopify URL with the website URL
        # Pattern: https://shopify-domain/path or http://shopify-domain/path
        pattern = rf'https?://{re.escape(shopify_domain)}'
        result = re.sub(pattern, f'https://{website_domain}', message)

        # Also handle cases where URL might be without protocol
        result = result.replace(shopify_domain, website_domain)

        # Clean up any double https:// that might have been introduced
        result = result.replace('https://https://', 'https://')
        result = result.replace('http://https://', 'https://')

        return result
    except Exception as e:
        return message
