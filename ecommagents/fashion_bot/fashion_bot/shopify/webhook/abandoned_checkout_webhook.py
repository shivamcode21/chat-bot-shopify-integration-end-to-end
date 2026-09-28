"""
Abandoned Checkout Webhook Handler for Shopify.
Sends Gupshup template when checkout is abandoned.
"""

import logging
from fastapi import APIRouter, Request, HTTPException
from typing import Optional, Dict, Any
from urllib.parse import urlparse, parse_qsl, urlencode

from fashion_bot.config_manager import aresolve_client_id
from fashion_bot.utils.client_identity_cache import aget_client_id_by_shop_domain as _aget_cid_by_shop

from fashion_bot.shopify.webhook.templates_db import aget_client_template
from fashion_bot.shopify.webhook.gupshup_template_sender import asend_shopify_gupshup_template_generic
from fashion_bot.utils.client_id_utils import (
    is_client_blocklisted,
    is_shop_domain_blocklisted,
    is_abandoned_checkout_blocklisted,
)
from fashion_bot.utils.template_param_resolver import (
    build_template_params_from_context,
    normalize_template_param_order,
)
from fashion_bot.utils.whatsapp_api_version import (
    WHATSAPP_API_VERSION_ENTERPRISE,
    aget_whatsapp_api_version,
)
# Queue producer — dramatiq-free import; offloads the Gupshup send to a worker
# only when the 'cart' lane is enabled, else awaits inline (unchanged behaviour).
from fashion_bot.workers.enqueue import submit_or_inline
from fashion_bot.workers.config import JOB_CART_EVENT

logger = logging.getLogger(__name__)
router = APIRouter()


def _coerce_mapping(value: Any, field_name: str) -> Dict[str, Any]:
    """
    Normalize a webhook sub-payload to a mapping.

    Shopify payloads can occasionally send nested objects in inconsistent shapes.
    We accept:
    - dict -> returned as-is
    - list[dict, ...] -> first non-empty dict entry (or {} if none)
    - None / empty list -> {} (benign "field not provided" case)
    Everything else degrades to {} with a warning.

    Empty containers are expected for guest abandoned checkouts where the
    customer has not yet entered an address (Shopify sends `shipping_address`
    as `[]` or `[{}]`), so they are treated as a normal "no value" signal and
    logged at DEBUG rather than WARNING to avoid alert noise.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        # Prefer the first populated mapping; fall back to the first mapping.
        first_mapping: Optional[Dict[str, Any]] = None
        for item in value:
            if isinstance(item, dict):
                if first_mapping is None:
                    first_mapping = item
                if item:  # non-empty dict wins
                    return item
        if first_mapping is not None:
            # All mappings were empty (e.g. [{}]) -> equivalent to no value.
            logger.debug(
                "Webhook field provided empty mapping(s) in list; treating as no value",
                extra={"field_name": field_name, "field_type": "list"},
            )
            return first_mapping
        # Empty list, or list with no mappings at all.
        if value:
            # Non-empty list of non-mappings is genuinely unexpected.
            logger.warning(
                "Unexpected webhook field shape; list contains no mapping",
                extra={"field_name": field_name, "field_type": "list"},
            )
        else:
            logger.debug(
                "Webhook field provided as empty list; treating as no value",
                extra={"field_name": field_name, "field_type": "list"},
            )
        return {}
    if value is not None:
        logger.warning(
            "Unexpected webhook field shape; expected mapping",
            extra={"field_name": field_name, "field_type": type(value).__name__},
        )
    return {}


def extract_phone_number(webhook_data: Dict[str, Any]) -> Optional[str]:
    """
    Extract phone number from webhook data.
    Tries customer.phone first, then shipping_address.phone.
    
    Args:
        webhook_data: The abandoned checkout webhook payload
        
    Returns:
        Phone number or None
    """
    # Try customer.phone first
    customer = _coerce_mapping(webhook_data.get('customer', {}), 'customer')
    phone = customer.get('phone')
    
    if phone:
        # Clean up phone number (remove + if present)
        phone = phone.strip()
        if phone.startswith('+'):
            phone = phone[1:]
        return phone
    
    # Fallback to shipping_address.phone
    shipping_address = _coerce_mapping(webhook_data.get('shipping_address', {}), 'shipping_address')
    phone = shipping_address.get('phone')
    
    if phone:
        phone = phone.strip()
        if phone.startswith('+'):
            phone = phone[1:]
        return phone

    return None


def _normalize_phone(value: Any) -> Optional[str]:
    """Normalize a raw phone value: strip whitespace and a single leading '+'.

    Returns ``None`` for missing/blank/non-string values. Pure and idempotent.
    """
    if not isinstance(value, str):
        return None
    phone = value.strip()
    if phone.startswith('+'):
        phone = phone[1:]
    return phone or None


def extract_phone_fallback(webhook_data: Dict[str, Any]) -> Optional[str]:
    """Secondary phone lookup for fields the primary extractor does not read.

    Guest abandoned-checkout webhooks usually carry the buyer's phone in the
    top-level ``phone`` field, ``sms_marketing_phone``, ``billing_address.phone``
    or ``customer.default_address.phone`` rather than ``customer.phone`` /
    ``shipping_address.phone``.

    This is purely additive: it is only invoked when
    :func:`extract_phone_number` returns nothing, so it cannot change existing
    behaviour. Pure and idempotent — no side effects.
    """
    # 1) Top-level checkout phone (most common for guest checkouts).
    phone = _normalize_phone(webhook_data.get('phone'))
    if phone:
        return phone

    # 2) SMS marketing consent phone.
    phone = _normalize_phone(webhook_data.get('sms_marketing_phone'))
    if phone:
        return phone

    # 3) billing_address.phone.
    billing_address = _coerce_mapping(webhook_data.get('billing_address', {}), 'billing_address')
    phone = _normalize_phone(billing_address.get('phone'))
    if phone:
        return phone

    # 4) customer.default_address.phone.
    customer = _coerce_mapping(webhook_data.get('customer', {}), 'customer')
    default_address = _coerce_mapping(customer.get('default_address', {}), 'customer.default_address')
    phone = _normalize_phone(default_address.get('phone'))
    if phone:
        return phone

    return None


# Shopify topics that deliver a *cart* payload. These share the same endpoint as
# the ``checkouts/*`` topics but structurally contain no customer/address/phone
# fields, so abandoned-checkout recovery can never act on them.
_CART_TOPICS = frozenset({'carts/create', 'carts/update'})


def is_cart_topic(topic: Optional[str]) -> bool:
    """True if the Shopify webhook topic is a cart event (no contact fields).

    Pure and idempotent. Used to short-circuit cart deliveries before phone
    extraction so they don't produce misleading "no phone" warnings.
    """
    return bool(topic) and topic.strip().lower() in _CART_TOPICS


def extract_customer_name(webhook_data: Dict[str, Any]) -> str:
    """
    Extract customer first name from webhook data.
    Tries shipping_address.first_name first, then customer.first_name.
    
    Args:
        webhook_data: The abandoned checkout webhook payload
        
    Returns:
        Customer first name or 'Customer' as fallback
    """
    # Try shipping_address.first_name first (more likely to be populated)
    shipping_address = _coerce_mapping(webhook_data.get('shipping_address', {}), 'shipping_address')
    first_name = shipping_address.get('first_name')
    
    if first_name and first_name.strip():
        return first_name.strip()
    
    # Fallback to customer.first_name
    customer = _coerce_mapping(webhook_data.get('customer', {}), 'customer')
    first_name = customer.get('first_name')
    
    if first_name and first_name.strip():
        return first_name.strip()
    
    # Default fallback
    return 'Customer'


def extract_product_url(webhook_data: Dict[str, Any]) -> str:
    """
    Extract product URL from the first line item in checkout.
    Constructs Shopify product URL with variant.
    
    Args:
        webhook_data: The abandoned checkout webhook payload
        
    Returns:
        Product URL with variant or empty string
    """
    line_items = webhook_data.get('line_items', [])

    if not line_items:
        return ''

    # Get first line item
    first_item = line_items[0]

    # 1) Prefer explicit URL fields if present in the payload
    #    Some Shopify webhooks include a direct product or line item URL
    for key in ("url", "product_url"):
        direct_url = first_item.get(key)
        if isinstance(direct_url, str) and direct_url.strip():
            return direct_url.strip()

    product_obj = first_item.get('product') or {}
    direct_url = product_obj.get('url')
    if isinstance(direct_url, str) and direct_url.strip():
        return direct_url.strip()

    # 2) Fall back to constructing from product and variant IDs
    product_id = first_item.get('product_id')
    variant_id = first_item.get('variant_id')

    if not product_id:
        return ''

    if variant_id:
        return f"https://groovee.in/products/{product_id}?variant={variant_id}"

    return f"https://groovee.in/products/{product_id}"


def extract_image_url(webhook_data: Dict[str, Any]) -> str:
    """
    Extract a direct image URL from the first line item if available.
    Prefer explicit image fields and return empty string if not found.
    """
    line_items = webhook_data.get('line_items', [])
    if not line_items:
        return ''

    first_item = line_items[0]

    # Look for common image structures
    image_obj = first_item.get('image') or first_item.get('featured_image') or {}
    if isinstance(image_obj, dict):
        for key in ("original_src", "src", "url"):  # try common keys
            val = image_obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    # Some payloads may embed image URL directly under keys
    for key in ("image_url", "image"):  # if image is a string
        val = first_item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    return ''


def extract_abandoned_checkout_url(webhook_data: Dict[str, Any]) -> str:
    """
    Prefer the checkout recovery/abandoned URL provided by Shopify webhook.
    Fallbacks try a few common keys. If none present, return empty string.
    """
    for key in ("abandoned_checkout_url", "recovery_url", "checkout_url", "web_url", "url"):
        val = webhook_data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ''


def transform_checkout_url_to_param(checkout_url: str) -> str:
    """
    Convert full checkout recovery URL to param value by:
    - Removing scheme and domain (e.g., https://groovee.in/)
    - Dropping the 'locale' query parameter (any value)
    - Returning path + filtered query string (if any)
    Example:
      https://groovee.in/896.../recover?key=ABC&locale=en-IN -> 896.../recover?key=ABC
    """
    try:
        if not checkout_url or not isinstance(checkout_url, str):
            return ''
        parsed = urlparse(checkout_url)
        # Filter out locale param (case-insensitive key match)
        query_pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != 'locale']
        query_str = urlencode(query_pairs, doseq=True)
        # Build path + query (no scheme/domain)
        base = parsed.path.lstrip('/')  # drop leading '/'
        return f"{base}{('?' + query_str) if query_str else ''}"
    except Exception:
        return ''


async def aget_client_id_from_shop_domain(shop_domain: str) -> Optional[str]:
    """Resolve shop domain to client_id via unified tiered cache."""
    return await _aget_cid_by_shop(shop_domain)

@router.post("/cart/checkout/webhook")
async def abandoned_checkout_webhook(request: Request):
    """
    Handle abandoned checkout webhook from Shopify.
    Sends ABANDON_CHECKOUT template to the customer.
    
    Webhook payload contains:
    - customer.phone or shipping_address.phone
    - shipping_address.first_name for name
    - Other checkout details
    """
    try:
        # ============================================================================
        # STEP 1: Parse Webhook Data
        # ============================================================================
        print("\n" + "="*80)
        print("[ABANDONED_CHECKOUT] 📦 New webhook received")
        print("="*80)
        
        webhook_data = await request.json()
        if not isinstance(webhook_data, dict):
            logger.error(
                "[ABANDONED_CHECKOUT] Invalid webhook payload type",
                extra={"payload_type": type(webhook_data).__name__},
            )
            raise HTTPException(status_code=400, detail="Invalid webhook payload")
        
        checkout_id = webhook_data.get('id', 'unknown')
        checkout_token = webhook_data.get('token', '')
        cart_token = webhook_data.get('cart_token', '')
        created_at = webhook_data.get('created_at', '')
        updated_at = webhook_data.get('updated_at', '')
        shopify_shop_domain = request.headers.get('X-Shopify-Shop-Domain', 'N/A')
        shopify_webhook_id = request.headers.get('X-Shopify-Webhook-Id', None)
        print(f"  X-Shopify-Shop-Domain:  {shopify_shop_domain} ⭐")

        if is_shop_domain_blocklisted(shopify_shop_domain):
            logger.info(
                f"[ABANDONED_CHECKOUT] 🚫 Blocked webhook for blocklisted Shopify domain: "
                f"{shopify_shop_domain}"
            )
            return {"status": "blocked", "message": "Shop domain is blocklisted"}

        client_id = None
        if shopify_shop_domain and shopify_shop_domain != 'N/A':
            client_id = await aget_client_id_from_shop_domain(shopify_shop_domain)

        if is_client_blocklisted(client_id):
            logger.info(f"[ABANDONED_CHECKOUT] 🚫 Blocked webhook for blocklisted client_id: {client_id}")
            return {"status": "blocked", "message": "Client is blocklisted"}

        # Scoped opt-out: client doesn't want abandoned-checkout recovery
        # (e.g. no template configured, using a different channel) but should
        # keep receiving order/shipment/product webhooks normally. Gate this
        # before any parsing/template/DB work below.
        if is_abandoned_checkout_blocklisted(client_id):
            logger.info(
                f"[ABANDONED_CHECKOUT] 🚫 Skipped — client opted out via "
                f"BLOCKLIST_ABANDONED_CHECKOUT_CLIENTS: {client_id}"
            )
            return {"status": "skipped", "reason": "abandoned_checkout_disabled_for_client"}

        # Cart events (carts/create, carts/update) are subscribed to this same
        # endpoint but carry no customer/address/phone, so recovery cannot act on
        # them. Skip early to avoid misleading "no phone" warnings and wasted work.
        shopify_topic = request.headers.get('X-Shopify-Topic')
        if is_cart_topic(shopify_topic):
            logger.info(
                f"[ABANDONED_CHECKOUT] ⏭️ Skipping cart event (no contact fields): "
                f"topic={shopify_topic} id={checkout_id}"
            )
            return {
                "status": "skipped",
                "reason": "cart_event_no_contact_fields",
                "topic": shopify_topic,
            }

        print(f"[ABANDONED_CHECKOUT] 🆔 Checkout ID: {checkout_id}")
        logger.info(f"[ABANDONED_CHECKOUT] Checkout ID: {checkout_id}")
        
        print(f"[ABANDONED_CHECKOUT] 🎫 Checkout Token: {checkout_token}")
        logger.info(f"[ABANDONED_CHECKOUT] Checkout Token: {checkout_token}")
        
        print(f"[ABANDONED_CHECKOUT] 🛒 Cart Token: {cart_token}")
        logger.info(f"[ABANDONED_CHECKOUT] Cart Token: {cart_token}")
        
        print(f"[ABANDONED_CHECKOUT] 📅 Created: {created_at}")
        logger.info(f"[ABANDONED_CHECKOUT] Created: {created_at}")
        
        print(f"[ABANDONED_CHECKOUT] 🔄 Updated: {updated_at}")
        logger.info(f"[ABANDONED_CHECKOUT] Updated: {updated_at}")
        
        # ============================================================================
        # STEP 2: Extract Phone Number
        # ============================================================================
        print("\n[PHONE_EXTRACTION] 📞 Extracting phone number...")
        logger.info(f"[PHONE_EXTRACTION] Extracting phone number from checkout {checkout_id}")
        
        phone = extract_phone_number(webhook_data)

        # Additive fallback: guest checkouts usually carry the phone in the
        # top-level `phone` / `billing_address.phone` / `customer.default_address.phone`
        # fields, which the primary extractor does not read. Only runs when the
        # primary lookup found nothing, so existing behaviour is unchanged.
        if not phone:
            phone = extract_phone_fallback(webhook_data)
            if phone:
                logger.info(
                    f"[PHONE_EXTRACTION] Phone resolved via fallback field for checkout {checkout_id}"
                )

        if not phone:
            print(f"[PHONE_EXTRACTION] ❌ No phone number found in checkout")
            logger.warning(
                f"[PHONE_EXTRACTION] guest_checkout_phone_not_available for checkout {checkout_id}"
            )
            
            # Log what we checked (primary + fallback fields) for diagnostics.
            customer = _coerce_mapping(webhook_data.get('customer', {}), 'customer')
            shipping_address = _coerce_mapping(webhook_data.get('shipping_address', {}), 'shipping_address')
            billing_address = _coerce_mapping(webhook_data.get('billing_address', {}), 'billing_address')
            default_address = _coerce_mapping(customer.get('default_address', {}), 'customer.default_address')
            print(f"[PHONE_EXTRACTION]    Checked customer.phone: {customer.get('phone', 'N/A')}")
            print(f"[PHONE_EXTRACTION]    Checked shipping_address.phone: {shipping_address.get('phone', 'N/A')}")
            print(f"[PHONE_EXTRACTION]    Checked top-level phone: {webhook_data.get('phone', 'N/A')}")
            print(f"[PHONE_EXTRACTION]    Checked billing_address.phone: {billing_address.get('phone', 'N/A')}")
            logger.info(
                f"[PHONE_EXTRACTION] Checked customer.phone: {customer.get('phone', 'N/A')}, "
                f"shipping_address.phone: {shipping_address.get('phone', 'N/A')}, "
                f"top_level_phone: {webhook_data.get('phone', 'N/A')}, "
                f"billing_address.phone: {billing_address.get('phone', 'N/A')}, "
                f"customer.default_address.phone: {default_address.get('phone', 'N/A')}"
            )
            
            return {
                "status": "skipped",
                "reason": "guest_checkout_phone_not_available",
                "checkout_id": checkout_id
            }
        
        print(f"[PHONE_EXTRACTION] ✅ Phone found: {phone}")
        logger.info(f"[PHONE_EXTRACTION] Phone: {phone}")
        
        # ============================================================================
        # STEP 3: Extract Customer Name
        # ============================================================================
        print("\n[NAME_EXTRACTION] 👤 Extracting customer name...")
        logger.info(f"[NAME_EXTRACTION] Extracting customer name from checkout {checkout_id}")
        
        customer_name = extract_customer_name(webhook_data)
        print(f"[NAME_EXTRACTION] ✅ Customer name: {customer_name}")
        logger.info(f"[NAME_EXTRACTION] Customer name: {customer_name}")
        
        # ============================================================================
        # STEP 4: Extract URLs (Checkout URL & Product URL)
        # ============================================================================
        print("\n[URL_EXTRACTION] 🔗 Extracting URLs...")
        logger.info(f"[URL_EXTRACTION] Extracting URLs from checkout {checkout_id}")
        
        # Extract checkout URL
        checkout_url = extract_abandoned_checkout_url(webhook_data)
        if checkout_url:
            print(f"[URL_EXTRACTION] ✅ Checkout URL found:")
            print(f"  → {checkout_url}")
        else:
            print(f"[URL_EXTRACTION] ⚠️  No checkout URL found")
        logger.info(f"[URL_EXTRACTION] Checkout URL: {checkout_url or 'N/A'}")
        
        # Transform checkout URL to param (strip domain and locale)
        checkout_param = transform_checkout_url_to_param(checkout_url) if checkout_url else ''
        if checkout_param:
            print(f"[URL_EXTRACTION] ✅ Checkout param (stripped):")
            print(f"  → {checkout_param}")
        else:
            print(f"[URL_EXTRACTION] ⚠️  No checkout param generated")
        logger.info(f"[URL_EXTRACTION] Checkout param (stripped): {checkout_param or 'N/A'}")
        
        # Extract product URL as fallback
        product_url = extract_product_url(webhook_data)
        if product_url:
            print(f"[URL_EXTRACTION] ✅ Product URL (fallback):")
            print(f"  → {product_url}")
        else:
            print(f"[URL_EXTRACTION] ⚠️  No product URL found")
        logger.info(f"[URL_EXTRACTION] Product URL (fallback): {product_url or 'N/A'}")
        
        # ============================================================================
        # STEP 5: Get Client ID
        # ============================================================================
        print("\n[CLIENT_CONFIG] 🏢 Getting client configuration...")

        # Fallback to default client_id if not found
        if not client_id:
            client_id = await aresolve_client_id()
            logging.warning(f"Using default client_id: {client_id} for shop domain: {shopify_shop_domain}")
        else:
            logging.info(f"[CLIENT: {client_id}] Mapped shop domain: {shopify_shop_domain}")

        
        # ============================================================================
        # STEP 6: Look Up Template from Database
        # ============================================================================
        print("\n[TEMPLATE_LOOKUP] 📋 Looking up template in database...")
        
        event_key = 'ABANDON_CHECKOUT'
        channel = 'shopify'
        
        print(f"[TEMPLATE_LOOKUP] 🔍 Searching for:")
        print(f"  - Event Key: {event_key}")
        print(f"  - Channel: {channel}")
        print(f"  - Client ID: {client_id}")
        logger.info(f"[TEMPLATE_LOOKUP] Searching for event_key={event_key}, channel={channel}, client_id={client_id}")
        
        template_config = await aget_client_template(client_id, channel, event_key)
        
        if not template_config:
            print(f"[TEMPLATE_LOOKUP] ❌ No template found in database")
            # Expected/handled state, not a failure — client hasn't configured
            # this template yet. Webhook is skipped cleanly below.
            logger.info(f"[TEMPLATE_LOOKUP] No template configured for {event_key}, skipping client_id={client_id}")
            return {
                "status": "skipped",
                "reason": f"No template configured for {event_key}",
                "checkout_id": checkout_id,
                "phone": phone
            }
        
        # Extract template details
        template_id = template_config.get('template_id')
        image_url_db = template_config.get('image_url')
        param_order = template_config.get('param_order', [])
        template_name = template_config.get('template_name', 'abandoned_checkout_reminder')
        
        print(f"[TEMPLATE_LOOKUP] ✅ Template found:")
        print(f"  - Template ID: {template_id}")
        print(f"  - Template Name: {template_name}")
        print(f"  - Param Order: {param_order}")
        print(f"  - DB Image URL: {image_url_db or 'N/A'}")
        logger.info(f"[TEMPLATE_LOOKUP] Template ID: {template_id}, Name: {template_name}, Params: {param_order}")
        
        # ============================================================================
        # STEP 7: Build Template Parameters
        # ============================================================================
        print("\n[PARAM_BUILD] 🔧 Building template parameters...")
        logger.info(f"[PARAM_BUILD] Building parameters for template {template_id}")
        
        is_enterprise_template_flow = (
            await aget_whatsapp_api_version(client_id)
            == WHATSAPP_API_VERSION_ENTERPRISE
        )
        if is_enterprise_template_flow:
            final_url = checkout_param or checkout_url or product_url or ''
            params = build_template_params_from_context(
                param_order,
                {
                    "customer_name": customer_name,
                    "abandoned_checkout_url": final_url,
                    "product_url": final_url,
                },
            )
            for idx, (param_key, param_value) in enumerate(
                zip(normalize_template_param_order(param_order), params), 1
            ):
                print(f"[PARAM_BUILD] ✅ Param {idx} ({param_key}): {param_value or '(empty)'}")
        else:
            params = []
            for idx, param_key in enumerate(param_order, 1):
                if param_key == 'name' or param_key == 'first_name':
                    params.append(customer_name)
                    print(f"[PARAM_BUILD] ✅ Param {idx} ({param_key}): {customer_name}")
                elif param_key == 'abandoned_checkout_url' or param_key == 'product_url':
                    # Prefer stripped checkout param; fallback to full checkout or product URL
                    final_url = checkout_param or checkout_url or product_url or ''
                    params.append(final_url)
                    print(f"[PARAM_BUILD] ✅ Param {idx} ({param_key}): {final_url or '(empty)'}")
                else:
                    # Unknown param, add empty string
                    params.append('')
                    print(f"[PARAM_BUILD] ⚠️  Param {idx} ({param_key}): (empty - unknown param)")
        
        logger.info(f"[PARAM_BUILD] Final params: {params}")
        
        # ============================================================================
        # STEP 8: Determine Image URL (DB vs Webhook)
        # ============================================================================
        print("\n[IMAGE_SELECT] 🖼️  Selecting image URL...")
        logger.info(f"[IMAGE_SELECT] Determining image URL for template")
        
        # Extract image from webhook
        image_url_webhook = extract_image_url(webhook_data)
        
        # Choose image URL: prefer DB image; fallback to webhook image; allow empty (no media)
        image_url = image_url_db or image_url_webhook or ''
        
        print(f"[IMAGE_SELECT] Options evaluated:")
        print(f"  - DB Image URL: {image_url_db or 'N/A'}")
        print(f"  - Webhook Image URL: {image_url_webhook or 'N/A'}")
        print(f"[IMAGE_SELECT] ✅ Final Image URL: {image_url or '(no image - text only)'}")
        logger.info(f"[IMAGE_SELECT] DB: {image_url_db or 'N/A'}, Webhook: {image_url_webhook or 'N/A'}, Final: {image_url or 'N/A'}")

        # ============================================================================
        # STEP 9: Send Template via Gupshup
        # ============================================================================
        print("\n[GUPSHUP_SEND] 📤 Sending template to Gupshup...")
        print(f"[GUPSHUP_SEND] Recipient: {phone}")
        print(f"[GUPSHUP_SEND] Template ID: {template_id}")
        print(f"[GUPSHUP_SEND] Parameters: {params}")
        print(f"[GUPSHUP_SEND] Image URL: {image_url or '(no image)'}")
        logger.info(f"[GUPSHUP_SEND] Sending to {phone} - Template: {template_id}, Params: {params}, Image: {image_url or 'N/A'}")

        # Send template via generic sender. Offloaded to a worker when the 'cart'
        # lane is enabled (cheap extraction above stays inline); inline otherwise.
        result = await submit_or_inline(
            JOB_CART_EVENT,
            {
                "destination_phone": phone,
                "template_id": template_id,
                "params": params,
                "image_url": image_url,
                "client_id": client_id,
                "event_key": event_key,
                "template_name": template_name,
                "shopify_webhook_id": shopify_webhook_id,
            },
            lambda: asend_shopify_gupshup_template_generic(
                destination_phone=phone,
                template_id=template_id,
                params=params,
                image_url=image_url,
                client_id=client_id,
                event_key=event_key,
                template_name=template_name
            ),
        )

        # ============================================================================
        # STEP 10: Return Response
        # ============================================================================
        if result:
            print(f"\n[GUPSHUP_SEND] ✅ SUCCESS - Template sent to {phone}")
            print("="*80 + "\n")
            logger.info(f"[GUPSHUP_SEND] ✅ Template sent successfully to {phone}")
            return {
                "status": "success",
                "message": "Abandoned checkout template sent",
                "checkout_id": checkout_id,
                "phone": phone,
                "template_id": template_id,
                "params": params
            }
        else:
            print(f"\n[GUPSHUP_SEND] ❌ FAILED - Could not send template to {phone}")
            print("="*80 + "\n")
            logger.error(f"[GUPSHUP_SEND] ❌ Generic sender failed for {phone}")
            return {
                "status": "error",
                "message": "Failed to send template",
                "checkout_id": checkout_id,
                "phone": phone
            }
            
    except Exception as e:
        print(f"\n[ABANDONED_CHECKOUT] ❌ EXCEPTION: {str(e)}")
        print("="*80 + "\n")
        logger.error(f"[ABANDONED_CHECKOUT] Error processing webhook: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing abandoned checkout webhook: {str(e)}")
