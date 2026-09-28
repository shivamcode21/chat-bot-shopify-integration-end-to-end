"""
Tool Helper Functions

This module contains internal helper functions used by tools.py.
These functions are NOT exposed as MCP tools - they are implementation details.

Helper categories:
1. Client/State Management: _get_client_id_from_state, get_order_prefix
2. URL Validation: get_allowed_urls, get_allowed_domains, is_url_allowed
3. Order ID Normalization: normalize_order_id_variants
4. Order Status Classification: classify_status
5. DTO Creators: create_order_info_dto, create_order_status_summary_dto, create_delivery_timeline_dto
6. Date Formatting: format_etd_date
7. Phone Validation: validate_phone_number_access
8. Pincode Validation: validate_indian_pincode
"""

import logging
import re
from datetime import datetime
from typing import List, Dict, Optional, Any

from fashion_bot.config_manager import aget_config
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.client_context import get_client_id

logger = logging.getLogger(__name__)


# =============================================================================
# CLIENT/STATE MANAGEMENT
# =============================================================================

def _get_client_id_from_state(state: Optional[dict] = None) -> Optional[str]:
    """Extract client_id from state if available, otherwise from context.
    
    Args:
        state: Optional state dictionary
        
    Returns:
        client_id string or None
    """
    # Try to get from state first
    if state and isinstance(state, dict):
        client_id = state.get("client_id")
        if client_id:
            log_with_trace_id(state, f"✅ Got client_id from state: {client_id}", "info")
            return client_id
        else:
            log_with_trace_id(state, f"⚠️ state exists but client_id is None in state", "warning")
    else:
        if state is not None:  # Only log if state was explicitly passed as non-dict
            log_with_trace_id(None, f"⚠️ state parameter is None or not a dict: {type(state)}", "warning")

    # Fallback to shared context variable (for LangChain tools)
    context_client_id = get_client_id()
    log_with_trace_id(state, f"📥 Got client_id from context: {context_client_id}", "info")
    return context_client_id


async def get_order_prefix(state: Optional[dict] = None):
    """Get order prefix from database configuration

    Args:
        state: Optional state dictionary for multi-client support

    Returns:
        Order prefix string (empty if not configured)
        Never returns a hardcoded default - prefix must be configured in DB.
    """
    # Get client_id from state or context
    client_id = _get_client_id_from_state(state)

    try:
        prefix = await aget_config('order_prefix', client_id=client_id, default=None)
        if prefix is None:
            logger.warning(f"order_prefix NOT CONFIGURED for client {client_id} - please add to client_configs table")
            return ""
        return prefix.lower() if prefix else ""
    except Exception as e:
        logger.warning(f"Failed to get order_prefix: {e}")
        return ""


# =============================================================================
# URL VALIDATION
# =============================================================================

async def get_allowed_urls(state: Optional[dict] = None):
    """Get list of allowed URLs from website config

    Args:
        state: Optional state dictionary for multi-client support

    Returns:
        List of allowed URLs
    """
    # Get client_id from state or context
    client_id = _get_client_id_from_state(state)

    website_config = await aget_config('website_urls', client_id=client_id)
    
    if isinstance(website_config, dict):
        # Extract all URL values from the dict
        return list(website_config.values())
    elif isinstance(website_config, str):
        return [website_config]
    return []


async def get_allowed_domains(state: Optional[dict] = None):
    """Get list of allowed domains from website config

    Args:
        state: Optional state dictionary for multi-client support

    Returns:
        List of allowed domains
    """
    # Get allowed URLs using state
    allowed_urls = await get_allowed_urls(state=state)
    domains = []
    
    for url in allowed_urls:
        domain_match = re.search(r'https?://([^/]+)', url)
        if domain_match:
            domains.append(domain_match.group(1))
    
    return domains


async def is_url_allowed(url, state: Optional[dict] = None):
    """Check if URL is from allowed domains

    Args:
        url: URL to validate
        state: Optional state dictionary for multi-client support

    Returns:
        True if URL is from allowed domain, False otherwise
    """
    # Get allowed domains using state
    allowed_domains = await get_allowed_domains(state=state)
    
    for domain in allowed_domains:
        if domain in url:
            return True
    return False


# =============================================================================
# ORDER ID NORMALIZATION
# =============================================================================

async def normalize_order_id_variants(input_text, state: Optional[dict] = None):
    """
    Return a list of possible order name variants to try in Shopify:
    - with and without #
    - lower and upper case
    - handle exchange order IDs (e.g., 8004-EXC)

    Args:
        input_text: Order ID text to normalize
        state: Optional state dictionary for multi-client support

    Returns:
        List of possible order ID variants
    """
    # Handle exchange order IDs (e.g., 8004-EXC, 1234-EXC)
    exchange_match = re.search(r'(\d+)-EXC', input_text, re.IGNORECASE)
    if exchange_match:
        num = exchange_match.group(1)
        return [
            f"{num}-EXC",  # Original format
            f"#{num}-EXC",  # With hash
            num  # Just the number
        ]

    # Handle order prefix format orders (e.g., gv1234, GV1234) using dynamic config
    order_prefix = await get_order_prefix(state=state)
    prefix_match = re.search(rf'{order_prefix}(\d+)', input_text, re.IGNORECASE)
    if prefix_match:
        num = prefix_match.group(1)
        base = f"{order_prefix}{num}"
        return [
            f"#{order_prefix}{num}", f"{order_prefix.upper()}{num}", f"#{order_prefix.upper()}{num}", f"{order_prefix}{num}", f"{order_prefix.upper()}{num}", num
        ]
    
    # fallback: try as is, and also try with order prefix for numeric IDs
    variants = [input_text]
    
    # If it's a plain number, also try order prefix variants
    if input_text.isdigit():
        order_prefix = await get_order_prefix(state=state)
        variants.extend([
            f"#{order_prefix}{input_text}", f"{order_prefix.upper()}{input_text}", f"#{order_prefix.upper()}{input_text}", f"{order_prefix}{input_text}"
        ])
    
    return variants


# =============================================================================
# ORDER STATUS CLASSIFICATION
# =============================================================================

def classify_status(partner_status, cancelled_at, fulfillment_status):
    """Classify order status based on Shiprocket and Shopify status values.
    
    Args:
        partner_status: Status from Shiprocket
        cancelled_at: Cancellation timestamp from Shopify
        fulfillment_status: Fulfillment status from Shopify
        
    Returns:
        Normalized status string
    """
    partner_status = (partner_status or "").upper().strip()
    cancelled_at = (cancelled_at or "").strip()
    fulfillment_status = (fulfillment_status or "").strip()

    # Check both Shopify cancelled_at and Shiprocket status for cancellation
    if cancelled_at:
        return "Cancelled"
    if partner_status in {"CANCELED", "CANCELLED"}:
        return "Cancelled"
    if partner_status == "DELIVERED":
        return "Delivered"
    if partner_status in {
        "IN TRANSIT", "IN TRANSIT-EN-ROUTE", "OUT FOR DELIVERY", "PICKED UP",
        "MISROUTED","REACHED AT DESTINATION HUB", "SHIPPED"
    }:
        return "In Transit"
    if re.search(r"RTO|REACHED BACK TO SELLER CITY", partner_status):
        return "RTO"
    if re.search(r"RETURN", partner_status):
        return "Return"
    if partner_status in {"PICKUP SCHEDULED", "PICKUP RESCHEDULED", "PICKUP EXCEPTION", "OUT FOR PICKUP"}:
        return "Not Yet Dispatched"
    if fulfillment_status == "":
        return "Not Yet Dispatched"
    return partner_status


class ShopifyRateLimitError(Exception):
    """Raised when Shopify returns 429 Too Many Requests."""
    def __init__(self, retry_after: float = 2.0):
        self.retry_after = retry_after
        super().__init__(f"Shopify rate limited, retry after {retry_after}s")


# =============================================================================
# DTO CREATORS
# =============================================================================

def create_order_info_dto(order_id: str, channel_order_id: str, status: str, partner_status: str, 
                         shipment_status: str, customer: str, delivery_date: Optional[str], 
                         out_for_delivery_date: Optional[str], items: List[str], courier: str, tracking_url: str, awb: Optional[str], 
                         products: List[str], created_at: str, updated_at: str, source: Optional[str] = None,
                         tracking_company: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """Create order info using the DTO structure.
    
    Args:
        order_id: Order ID
        channel_order_id: Channel order ID
        status: Order status
        partner_status: Shiprocket status
        shipment_status: Shipment status
        customer: Customer name
        delivery_date: Delivery date
        out_for_delivery_date: Out for delivery date
        items: List of items
        courier: Courier name
        tracking_url: Tracking URL
        awb: AWB number
        products: List of products
        created_at: Creation timestamp
        updated_at: Update timestamp
        source: Optional source identifier
        
    Returns:
        OrderInfoDTO dict
    """
    from fashion_bot.schema import OrderInfoDTO
    
    order_info: OrderInfoDTO = {
        "order_id": order_id,
        "channel_order_id": channel_order_id,
        "status": status,
        "partner_status": partner_status,
        "shipment_status": shipment_status,
        "customer": customer,
        "delivery_date": delivery_date,
        "out_for_delivery_date": out_for_delivery_date,
        "items": items,
        "courier": courier,
        "tracking_company": tracking_company or courier,
        "tracking_url": tracking_url,
        "awb": awb,
        "products": products,
        "created_at": created_at,
        "updated_at": updated_at
    }
    
    if source:
        order_info["source"] = source
    
    return order_info


def create_order_status_summary_dto(order_id: str, channel_order_id: str, status: str, partner_status: str,
                                  shipment_status: str, customer_name: str, fulfillment_status: str,
                                  fulfilled_items: List[str], pending_items: List[str], courier: str,
                                  tracking_url: str, awb: Optional[str], etd_date: Optional[str],
                                  delivery_date: Optional[str], out_for_delivery_date: Optional[str], cancelled_at: Optional[str], products: List[str],
                                  created_at: str, updated_at: str, source: Optional[str] = None) -> Dict[str, Any]:
    """Create order status summary using the extended DTO structure.
    
    Args:
        order_id: Order ID
        channel_order_id: Channel order ID  
        status: Order status
        partner_status: Shiprocket status
        shipment_status: Shipment status
        customer_name: Customer name
        fulfillment_status: Fulfillment status
        fulfilled_items: List of fulfilled items
        pending_items: List of pending items
        courier: Courier name
        tracking_url: Tracking URL
        awb: AWB number
        etd_date: Estimated delivery date
        delivery_date: Actual delivery date
        out_for_delivery_date: Out for delivery date
        cancelled_at: Cancellation timestamp
        products: List of products
        created_at: Creation timestamp
        updated_at: Update timestamp
        source: Optional source identifier
        
    Returns:
        OrderStatusSummaryDTO dict
    """
    from fashion_bot.schema import OrderStatusSummaryDTO
    
    order_info: OrderStatusSummaryDTO = {
        "order_id": order_id,
        "channel_order_id": channel_order_id,
        "status": status,
        "partner_status": partner_status,
        "shipment_status": shipment_status,
        "customer_name": customer_name,
        "fulfillment_status": fulfillment_status,
        "fulfilled_items": fulfilled_items,
        "pending_items": pending_items,
        "courier": courier,
        "tracking_url": tracking_url,
        "awb": awb,
        "etd_date": etd_date,
        "delivery_date": delivery_date,
        "out_for_delivery_date": out_for_delivery_date,
        "cancelled_at": cancelled_at,
        "products": products,
        "created_at": created_at,
        "updated_at": updated_at
    }
    
    if source:
        order_info["source"] = source
    
    return order_info


def create_delivery_timeline_dto(order_id: str, channel_order_id: str, status: str, partner_status: str,
                                shipment_status: str, customer_name: str, awb: Optional[str],
                                etd_date: Optional[str], formatted_etd: Optional[str], message: str,
                                courier: str, products: List[str], created_at: str, updated_at: str) -> Dict[str, Any]:
    """Create delivery timeline using the extended DTO structure.
    
    Args:
        order_id: Order ID
        channel_order_id: Channel order ID
        status: Order status
        partner_status: Shiprocket status
        shipment_status: Shipment status
        customer_name: Customer name
        awb: AWB number
        etd_date: Estimated delivery date
        formatted_etd: Formatted ETD string
        message: Timeline message
        courier: Courier name
        products: List of products
        created_at: Creation timestamp
        updated_at: Update timestamp
        
    Returns:
        DeliveryTimelineDTO dict
    """
    from fashion_bot.schema import DeliveryTimelineDTO
    
    order_info: DeliveryTimelineDTO = {
        "order_id": order_id,
        "channel_order_id": channel_order_id,
        "status": status,
        "partner_status": partner_status,
        "shipment_status": shipment_status,
        "customer_name": customer_name,
        "awb": awb,
        "etd_date": etd_date,
        "formatted_etd": formatted_etd,
        "message": message,
        "courier": courier,
        "products": products,
        "created_at": created_at,
        "updated_at": updated_at
    }
    
    return order_info


# =============================================================================
# DATE FORMATTING
# =============================================================================

def format_etd_date(raw_date):
    """Format estimated delivery date to human-readable format.
    
    Args:
        raw_date: Raw date string in various formats
        
    Returns:
        Formatted date string
    """
    try:
        dt = datetime.strptime(raw_date, "%d-%m-%Y %H:%M:%S")
        suffix = "th" if 11 <= dt.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(dt.day % 10, "th")
        hour = dt.strftime("%I").lstrip("0") or "12"
        minute = dt.strftime("%M")
        ampm = dt.strftime("%p")
        return f"{dt.day}{suffix} {dt.strftime('%B, %Y')} around {hour}:{minute} {ampm}"
    except:
        # Try alternative date formats
        try:
            # Handle format like "7 Aug 2025 08:08 AM"
            dt = datetime.strptime(raw_date, "%d %b %Y %I:%M %p")
            suffix = "th" if 11 <= dt.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(dt.day % 10, "th")
            hour = dt.strftime("%I").lstrip("0") or "12"
            minute = dt.strftime("%M")
            ampm = dt.strftime("%p")
            return f"{dt.day}{suffix} {dt.strftime('%B, %Y')} around {hour}:{minute} {ampm}"
        except:
            try:
                # Handle format like "7 Aug 2025"
                dt = datetime.strptime(raw_date, "%d %b %Y")
                suffix = "th" if 11 <= dt.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(dt.day % 10, "th")
                return f"{dt.day}{suffix} {dt.strftime('%B, %Y')}"
            except:
                # If all parsing fails, return the raw date as is
                return raw_date


# PHONE VALIDATION
# =============================================================================

def validate_phone_number_access(state_phone: str, order_phone: str, bypass_phone: str = "9012345678") -> bool:
    """
    Validate if the customer can access order information based on phone number.
    
    Args:
        state_phone: Phone number from state
        order_phone: Phone number from order details
        bypass_phone: Phone number that can bypass validation (default: 9012345678)
    
    Returns:
        bool: True if access is allowed, False otherwise
    """
    # Allow bypass for specific phone number
    if state_phone == bypass_phone:
        return True
    
    # Remove all non-digit characters for comparison
    clean_state_phone = re.sub(r'\D', '', state_phone) if state_phone else ""
    clean_order_phone = re.sub(r'\D', '', order_phone) if order_phone else ""
    
    # If either phone is empty, deny access
    if not clean_state_phone or not clean_order_phone:
        return False
    
    # Compare last 10 digits (Indian phone numbers)
    state_last_10 = clean_state_phone[-10:] if len(clean_state_phone) >= 10 else clean_state_phone
    order_last_10 = clean_order_phone[-10:] if len(clean_order_phone) >= 10 else clean_order_phone
    
    return state_last_10 == order_last_10


# =============================================================================
# PINCODE VALIDATION
# =============================================================================

def validate_indian_pincode(pincode: str) -> bool:
    """
    Validate if a pincode/postal code is valid (accepts any country format).
    This function now supports international postal codes, not just Indian pincodes.
    
    Args:
        pincode: The pincode/postal code string to validate.
        
    Returns:
        bool: True if valid postal code format, False otherwise.
    
    Rules:
    - Must be between 3-10 characters
    - Can contain alphanumeric characters (letters and numbers)
    - Can contain spaces and hyphens (for formats like UK, Canada, USA ZIP+4)
    - Supports various international formats:
      * India: 110001 (6 digits)
      * USA: 90210, 90210-1234 (5 or 9 digits with hyphen)
      * UK: SW1A 1AA (alphanumeric with space)
      * Canada: K1A 0B1 (alphanumeric with space)
      * Australia: 2000 (4 digits)
      * And many more...
    """
    if not pincode or not isinstance(pincode, str):
        return False

    # Remove leading/trailing whitespace
    pincode = pincode.strip()
    
    # Must be between 3-10 characters (covers most postal code formats worldwide)
    if len(pincode) < 3 or len(pincode) > 10:
        return False

    # Must contain at least some alphanumeric characters
    if not re.search(r'[A-Za-z0-9]', pincode):
        return False
    
    # Allow only alphanumeric, spaces, and hyphens
    if not re.match(r'^[A-Za-z0-9\s\-]+$', pincode):
        return False

    # Reject all same digits (111111, 222222, etc.)
    if len(set(pincode)) == 1:
        return False

    # Reject "000000"
    if pincode == "000000":
        return False

    # Reject common obviously fake patterns
    fake_patterns = [
        "123456", "654321", "112233", "221133", "332211",
        "121212", "131313", "141414", "151515", "161616",
        "171717", "181818", "191919", "202020", "212121",
        "000001", "100000"
    ]
    
    if pincode in fake_patterns:
        return False
    
    return True
