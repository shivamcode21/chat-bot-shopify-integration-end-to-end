from typing import Dict, Any
import re

from fashion_bot.utils.http_client import get_shared_async_http_client

def sanitize_name(name: str) -> str:
    """Allow only alphabets and spaces."""
    return re.sub(r'[^A-Za-z ]+', '', name or '').strip()

def truncate_address(addr: str) -> str:
    return (addr or '')[:80]

def generate_unique_order_id(base_id: str) -> str:
    # Use the format: '<base_order_id>-bot'
    return f"{base_id}-bot"

async def acreate_shiprocket_order(
    token: str,
    existing_order_data: Dict[str, Any],
    new_shipping: Dict[str, str],
) -> Dict[str, Any]:
    """
    Create a new Shiprocket order using existing order details, but with updated shipping fields.
    Args:
        token: Shiprocket API token
        existing_order_data: Dict from /orders/show/{order_id}
        new_shipping: Dict with keys: shipping_customer_name, shipping_last_name, shipping_address, shipping_address_2, shipping_city, shipping_pincode, shipping_country, shipping_state, shipping_phone
    Returns:
        Shiprocket API response as dict
    """
    # Prepare payload by copying all fields from existing order, then updating shipping fields
    payload = {}
    # Required fields from existing order
    base_order_id = existing_order_data.get("channel_order_id") or existing_order_data.get("id")
    payload["order_id"] = generate_unique_order_id(str(base_order_id))
    payload["order_date"] = existing_order_data.get("order_date") or existing_order_data.get("created_at")
    payload["pickup_location"] = "Delhi gr"
    payload["channel_id"] = existing_order_data.get("channel_id")
    payload["comment"] = existing_order_data.get("comment", "")
    payload["billing_customer_name"] = sanitize_name(existing_order_data.get("customer_name") or existing_order_data.get("billing_name"))
    payload["billing_last_name"] = sanitize_name(existing_order_data.get("billing_last_name", ""))
    payload["billing_address"] = truncate_address(existing_order_data.get("customer_address") or existing_order_data.get("billing_address"))
    payload["billing_address_2"] = truncate_address(existing_order_data.get("customer_address_2") or existing_order_data.get("billing_address_2", ""))
    payload["billing_city"] = existing_order_data.get("customer_city") or existing_order_data.get("billing_city")
    payload["billing_pincode"] = existing_order_data.get("customer_pincode") or existing_order_data.get("billing_pincode")
    payload["billing_state"] = existing_order_data.get("customer_state") or existing_order_data.get("billing_state")
    payload["billing_country"] = existing_order_data.get("customer_country") or existing_order_data.get("billing_country")
    payload["billing_email"] = existing_order_data.get("customer_email") or existing_order_data.get("billing_email")
    payload["billing_phone"] = existing_order_data.get("customer_phone") or existing_order_data.get("billing_phone")
    payload["shipping_is_billing"] = existing_order_data.get("shipping_is_billing", True)
    # Overwrite shipping fields with new_shipping, sanitize/truncate as needed
    payload["shipping_customer_name"] = sanitize_name(new_shipping.get("shipping_customer_name", ""))
    payload["shipping_last_name"] = sanitize_name(new_shipping.get("shipping_last_name", ""))
    payload["shipping_address"] = truncate_address(new_shipping.get("shipping_address", ""))
    payload["shipping_address_2"] = truncate_address(new_shipping.get("shipping_address_2", ""))
    payload["shipping_city"] = new_shipping.get("shipping_city", "")
    payload["shipping_pincode"] = new_shipping.get("shipping_pincode", "")
    payload["shipping_country"] = new_shipping.get("shipping_country", "")
    payload["shipping_state"] = new_shipping.get("shipping_state", "")
    # Always take shipping_email from original order if available
    payload["shipping_email"] = existing_order_data.get("customer_email") or existing_order_data.get("billing_email") or new_shipping.get("shipping_email", payload["billing_email"])
    payload["shipping_phone"] = new_shipping.get("shipping_phone", "")
    # Items
    payload["order_items"] = []
    for prod in existing_order_data.get("products", []):
        payload["order_items"].append({
            "name": prod.get("name"),
            "sku": prod.get("sku"),
            "units": prod.get("quantity", 1),
            "selling_price": str(prod.get("price", "")),
            "discount": str(prod.get("discount", "")),
            "tax": str(prod.get("tax", "")),
            "hsn": prod.get("hsn", "")
        })
    payload["payment_method"] = existing_order_data.get("payment_method", "Prepaid")
    payload["shipping_charges"] = float(existing_order_data.get("shipping_charges", 0))
    payload["giftwrap_charges"] = float(existing_order_data.get("giftwrap_charges", 0))
    payload["transaction_charges"] = float(existing_order_data.get("transaction_charges", 0))
    payload["total_discount"] = float(existing_order_data.get("discount", 0))
    payload["sub_total"] = float(existing_order_data.get("total", 0))
    # Extract from shipments if available
    shipments = existing_order_data.get("shipments")
    if isinstance(shipments, list) and shipments:
        shipment = shipments[0]  # Use the first shipment
        # Parse dimensions
        dims = shipment.get("dimensions", "")
        try:
            length, breadth, height = [int(x) for x in dims.split("x")]
        except Exception:
            length, breadth, height = 40, 30, 8  # new fallback defaults
        # Use volumetric_weight if present, else weight
        try:
            volumetric_weight = float(shipment.get("volumetric_weight", 0))
        except Exception:
            volumetric_weight = 0
        try:
            dead_weight = float(shipment.get("weight", 0))
        except Exception:
            dead_weight = 0
        weight = volumetric_weight if volumetric_weight > dead_weight else dead_weight
        if weight == 0:
            weight = 1.2  # new default
    else:
        # Fallback to new defaults
        length = 40
        breadth = 30
        height = 8
        weight = 1.2

    payload["length"] = length
    payload["breadth"] = breadth
    payload["height"] = height
    payload["weight"] = weight

    # Use the adhoc endpoint for manual order creation
    url = "https://apiv2.shiprocket.in/v1/external/orders/create/adhoc"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }
    client = await get_shared_async_http_client()
    resp = await client.post(url, json=payload, headers=headers, timeout=20)
    try:
        return resp.json()
    except Exception:
        return {"success": False, "error": resp.text}
