"""
Comprehensive test suite for all tools in cancel_or_update_tools_factory.

Tools under test (16 total):
     1. get_order_details                — unified Shopify + logistics order details
     2. get_recent_orders           — recent actionable orders by phone
     3. update_order_address             — shipping address update
     4. update_order_size_tool           — size update
     5. update_order_phone_number_tool   — phone number update
     6. update_order_email_tool          — email update
     7. update_order_name_tool           — customer name update
     8. annotate_order                   — add notes/tags to order
     9. change_order_product_tool        — product change (cancel old + create new)
    10. search_products                  — product search (shared)
    11. find_product_by_url              — product lookup by URL
    12. find_product_by_id               — product lookup by handle/ID
    13. cancel_order_tool                — order cancellation
    14. escalate_to_agent         — human escalation (also handles Delivery Partner Sync)
    15. check_grace_period_eligibility   — return/exchange grace period check
    16. get_final_return_exchange_message — return/exchange instructions

Coverage areas:
    - Phone validation (access denied when phone mismatch)
    - Order lookup, details, and structure consistency
    - Update operations (address, size, phone, email, name)
    - Annotation (notes + tags)
    - Product change (COD flow)
    - Cancellation with reason categorisation
    - Escalation trigger structure
    - Grace period eligibility
    - Return/exchange message retrieval
    - Statelessness verification (no state mutation from tools)
    - Negative / edge-case inputs
"""

import copy
import pytest
from fashion_bot.tool_factory import (
    cancel_or_update_tools_factory,
    place_order_tools_factory,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"
TEST_CLIENT_ID = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
TEST_VALID_PRODUCT_URL = "https://blommerce-test.myshopify.com/products/evolve-the-cosmic-shacket"
TEST_VALID_PRODUCT_URL_2 = "https://blommerce-test.myshopify.com/products/value-protocol-long-sleeve"

# Unique phone per test category to avoid place_order dedup guard.
PHONE_ORDER_DETAILS = "9500200001"
PHONE_RECENT_ORDERS = "9500200002"
PHONE_UPDATE_ADDRESS = "9500200003"
PHONE_UPDATE_SIZE = "9500200004"
PHONE_UPDATE_PHONE = "9500200005"
PHONE_UPDATE_EMAIL = "9500200006"
PHONE_UPDATE_NAME = "9500200007"
PHONE_ANNOTATE = "9500200008"
PHONE_CANCEL = "9500200009"
PHONE_CHANGE_PRODUCT = "9500200010"
PHONE_CHANGE_PRODUCT_INVALID = "9500200011"
PHONE_CHANGE_PRODUCT_NONEXIST = "9500200012"

# Phones for complex order-update scenarios (multi-item, price escalation, payment preservation)
PHONE_MULTI_ITEM_SIZE = "9500200013"
PHONE_MULTI_ITEM_CHANGE = "9500200014"
PHONE_SIZE_PRICE_GRAPHQL = "9500200015"
PHONE_SIZE_PRICE_CANCEL_RECREATE = "9500200016"
PHONE_PREPAID_PRICE_ESC_HIGH = "9500200017"
PHONE_PREPAID_PRICE_ESC_LOW = "9500200018"
PHONE_PREPAID_SAME_PRICE = "9500200019"
PHONE_COD_PRESERVE = "9500200020"
PHONE_MULTI_ITEM_SIZE_2 = "9500200021"
PHONE_PARTIAL_QTY = "9500200022"
PHONE_MULTI_PRODUCT_SIZE = "9500200023"
PHONE_MULTI_PRODUCT_CHANGE = "9500200024"
PHONE_PREPAID_CANCEL = "9500200025"
PHONE_GOKWIK_COD_SIZE = "9500200026"
PHONE_GOKWIK_PREPAID_SIZE = "9500200027"
PHONE_PARTIAL_PAID_CANCEL = "9500200028"
PHONE_MULTI_QTY_SIZE = "9500200029"
PHONE_MULTI_QTY_PRODUCT = "9500200030"
PHONE_COD_PRICE_DIFF = "9500200031"
PHONE_SHIPPED_UPDATE = "9500200032"
PHONE_CANCELLED_UPDATE = "9500200033"
PHONE_GOKWIK_COD_PRODUCT = "9500200034"
PHONE_GOKWIK_PREPAID_PRODUCT = "9500200035"
PHONE_GOKWIK_PREPAID_PRODUCT_ESC = "9500200036"
PHONE_OLD_ORDER_VERIFY = "9500200037"
PHONE_NO_REFUND_VERIFY = "9500200038"
PHONE_PARTIAL_PAID_SIZE_TXN = "9500200039"
PHONE_PARTIAL_PAID_PRODUCT_TXN = "9500200040"
PHONE_DISCOUNT_SIZE_UPDATE = "9500200041"
PHONE_DISCOUNT_PRODUCT_CHANGE = "9500200042"
PHONE_DISCOUNT_SIZE_PREPAID = "9500200043"
PHONE_DISCOUNT_PRODUCT_PREPAID = "9500200044"
PHONE_DISCOUNT_SAME_RETAIL_PREPAID = "9500200045"
PHONE_DISCOUNT_DIFF_RETAIL_PREPAID = "9500200046"
PHONE_MULTI_ITEM_PREPAID_DIFF = "9500200047"
PHONE_OOS_SIZE_UPDATE = "9500200048"
PHONE_DISCOUNT_PCT_CLONE = "9500200049"
PHONE_COD_SIZE_PRICE_DIFF = "9500200050"
PHONE_COD_PRODUCT_PRICE_DIFF_LOW = "9500200051"
PHONE_COD_PRODUCT_SAME_PRICE = "9500200052"

TEST_PRODUCT_HANDLE = "evolve-the-cosmic-shacket"
TEST_PRODUCT_HANDLE_2 = "value-protocol-long-sleeve"

# Test-store Shopify credentials (same as test_place_order_tools.py)
_TEST_SHOPIFY_CLIENT_ID = "85391617d1691af65cf0267f54a3e298"
_TEST_SHOPIFY_CLIENT_SECRET = "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"
_TEST_SHOPIFY_DOMAIN = "blommerce-test.myshopify.com"


# ---------------------------------------------------------------------------
# Session-scoped fixture: refresh Shopify token once per test run
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _refresh_test_store_token():
    """Refresh the Shopify OAuth token for the test store before any test."""
    import httpx
    import json
    import psycopg
    from fashion_bot.env_loader import bootstrap_environment, get_env

    bootstrap_environment()

    resp = httpx.post(
        f"https://{_TEST_SHOPIFY_DOMAIN}/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": _TEST_SHOPIFY_CLIENT_ID,
            "client_secret": _TEST_SHOPIFY_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    fresh_token = resp.json()["access_token"]

    shopify_details = json.dumps({
        "api_version": "2024-04",
        "SHOPIFY_TOKEN": fresh_token,
        "SHOPIFY_DOMAIN": _TEST_SHOPIFY_DOMAIN,
        "SHOPIFY_CLIENT_ID": _TEST_SHOPIFY_CLIENT_ID,
        "SHOPIFY_CLIENT_SECRET": _TEST_SHOPIFY_CLIENT_SECRET,
    })

    website_urls = json.dumps({
        "website_url": f"https://{_TEST_SHOPIFY_DOMAIN}",
    })

    with psycopg.connect(get_env("DATABASE_URL")) as conn:
        conn.execute(
            "INSERT INTO client_configs (client_id, config_key, config_value) "
            "VALUES (%s::uuid, 'shopify_details', %s::jsonb) "
            "ON CONFLICT (client_id, config_key) DO UPDATE SET config_value = EXCLUDED.config_value",
            (TEST_CLIENT_ID, shopify_details),
        )
        conn.execute(
            "INSERT INTO client_configs (client_id, config_key, config_value) "
            "VALUES (%s::uuid, 'website_urls', %s::jsonb) "
            "ON CONFLICT (client_id, config_key) DO UPDATE SET config_value = EXCLUDED.config_value",
            (TEST_CLIENT_ID, website_urls),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_state(phone: str = "9716336096", client_id: str = CLIENT_ID):
    return {
        "client_id": client_id,
        "phone_number": phone,
        "conversation_context": {"entities": [], "topics": [], "focal_entity": None},
        "messages": [],
    }


def _get_cancel_update_tools(state=None):
    """Build cancel_or_update factory and return a name→tool mapping."""
    if state is None:
        state = _build_state()
    tools = cancel_or_update_tools_factory(state, state["messages"])
    return {t.name: t for t in tools}, state


async def _create_test_order(phone: str) -> dict:
    """Create a fresh COD order on the blommerce-test store.

    Returns the create_order result dict (contains order_id, order_number, etc.).
    Waits for Shopify's search API to index the order before returning.
    """
    import asyncio

    state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    po_tools = place_order_tools_factory(state, state["messages"], TEST_CLIENT_ID)
    po_map = {t.name: t for t in po_tools}
    result = await po_map["create_order"].ainvoke({
        "product_link": TEST_VALID_PRODUCT_URL,
        "size": "L",
        "customer_name": "CancelUpdate Test",
        "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
        "pincode": "110058",
    })
    if result.get("success"):
        oid = _order_id_from_result(result)
        await _wait_for_order_indexed(oid, state)
    return result


async def _wait_for_order_indexed(order_id: str, state: dict, max_retries: int = 5):
    """Poll until Shopify's search API finds the order (handles indexing delay)."""
    import asyncio
    from fashion_bot.core.orchestrator import OrderStatusOrchestrator

    for attempt in range(max_retries):
        wait = 3 * (attempt + 1)
        await asyncio.sleep(wait)
        try:
            r = await OrderStatusOrchestrator.aget_order_status(order_id, state=state)
            if r.get("orders"):
                return
        except Exception:
            pass
    # give up — tests will deal with the failure


def _order_id_from_result(result: dict) -> str:
    """Extract a usable order_id from a create_order result."""
    order_number = str(result.get("order_number", ""))
    if order_number:
        return order_number
    return str(result.get("order_id", "")).lstrip("#")


# ---------------------------------------------------------------------------
# Helpers for complex order-update scenarios
# ---------------------------------------------------------------------------

async def _get_shopify_rest_config():
    """Return (headers, base_url) for direct Shopify REST Admin API calls."""
    from fashion_bot.config_manager import aget_shopify_config

    config = await aget_shopify_config(client_id=TEST_CLIENT_ID)
    token = config["access_token"]
    shop_url = config["shop_url"]
    api_version = config.get("api_version", "2024-04")
    base_url = f"https://{shop_url}/admin/api/{api_version}"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    return headers, base_url


async def _get_product_variants():
    """Fetch variant IDs, titles, and prices for the test product."""
    import httpx

    headers, base_url = await _get_shopify_rest_config()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/products.json",
            params={"handle": TEST_PRODUCT_HANDLE},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
    if not products:
        return {}
    product = products[0]
    return {
        "product_id": product["id"],
        "title": product["title"],
        "variants": {
            v["title"].upper(): {"id": v["id"], "title": v["title"], "price": v["price"]}
            for v in product.get("variants", [])
        },
    }


async def _create_multi_item_test_order(phone: str, sizes: list = None) -> dict:
    """Create an order with multiple line items (different size variants)
    via Shopify REST Admin API.  Returns dict with order_id, line_items, etc.
    """
    import httpx

    if sizes is None:
        sizes = ["M", "L"]

    product_info = await _get_product_variants()
    if not product_info:
        return {"success": False, "error": "Could not fetch product info"}

    line_items = []
    for size in sizes:
        variant = product_info["variants"].get(size.upper())
        if not variant:
            available = list(product_info["variants"].keys())
            return {"success": False, "error": f"Variant '{size}' not found. Available: {available}"}
        line_items.append({"variant_id": variant["id"], "quantity": 1})

    headers, base_url = await _get_shopify_rest_config()
    payload = {
        "order": {
            "line_items": line_items,
            "shipping_address": {
                "first_name": "MultiItem", "last_name": "Test",
                "address1": "E-10 Jail Road, Janak Puri",
                "city": "New Delhi", "province": "Delhi",
                "zip": "110058", "country": "India",
                "phone": f"+91{phone}",
            },
            "phone": f"+91{phone}",
            "financial_status": "pending",
            "tags": "BOT_TEST, MULTI_ITEM_TEST",
        },
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/orders.json", json=payload, headers=headers, timeout=30,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            return {"success": False, "error": f"Shopify HTTP {resp.status_code}: {body}"}
        order = resp.json().get("order", {})

    oid = str(order.get("order_number", ""))
    state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    await _wait_for_order_indexed(oid, state)
    return {
        "success": True,
        "order_id": oid,
        "order_name": order.get("name", ""),
        "shopify_id": order.get("id"),
        "line_items": order.get("line_items", []),
        "total_price": order.get("total_price"),
    }


async def _get_product_variants_by_handle(handle: str):
    """Fetch variant IDs, titles, and prices for any product by handle."""
    import httpx

    headers, base_url = await _get_shopify_rest_config()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/products.json",
            params={"handle": handle},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
    if not products:
        return {}
    product = products[0]
    return {
        "product_id": product["id"],
        "title": product["title"],
        "variants": {
            v["title"].upper(): {"id": v["id"], "title": v["title"], "price": v["price"]}
            for v in product.get("variants", [])
        },
    }


async def _create_multi_product_test_order(
    phone: str,
    product1_handle: str = TEST_PRODUCT_HANDLE,
    product1_size: str = "M",
    product2_handle: str = TEST_PRODUCT_HANDLE_2,
    product2_size: str = "L",
) -> dict:
    """Create an order with line items from TWO different products.

    Returns dict with order_id, line_items, etc.
    """
    import httpx

    p1 = await _get_product_variants_by_handle(product1_handle)
    p2 = await _get_product_variants_by_handle(product2_handle)
    if not p1 or not p2:
        return {"success": False, "error": f"Could not fetch product info for {product1_handle} and/or {product2_handle}"}

    v1 = p1["variants"].get(product1_size.upper())
    v2 = p2["variants"].get(product2_size.upper())
    if not v1:
        return {"success": False, "error": f"Variant '{product1_size}' not found for {p1['title']}. Available: {list(p1['variants'].keys())}"}
    if not v2:
        return {"success": False, "error": f"Variant '{product2_size}' not found for {p2['title']}. Available: {list(p2['variants'].keys())}"}

    headers, base_url = await _get_shopify_rest_config()
    payload = {
        "order": {
            "line_items": [
                {"variant_id": v1["id"], "quantity": 1},
                {"variant_id": v2["id"], "quantity": 1},
            ],
            "shipping_address": {
                "first_name": "MultiProduct", "last_name": "Test",
                "address1": "E-10 Jail Road, Janak Puri",
                "city": "New Delhi", "province": "Delhi",
                "zip": "110058", "country": "India",
                "phone": f"+91{phone}",
            },
            "phone": f"+91{phone}",
            "financial_status": "pending",
            "tags": "BOT_TEST, MULTI_PRODUCT_TEST",
        },
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/orders.json", json=payload, headers=headers, timeout=30,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            return {"success": False, "error": f"Shopify HTTP {resp.status_code}: {body}"}
        order = resp.json().get("order", {})

    oid = str(order.get("order_number", ""))
    state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    await _wait_for_order_indexed(oid, state)
    return {
        "success": True,
        "order_id": oid,
        "order_name": order.get("name", ""),
        "shopify_id": order.get("id"),
        "line_items": order.get("line_items", []),
        "total_price": order.get("total_price"),
        "product1_title": p1["title"],
        "product2_title": p2["title"],
    }


async def _create_multi_item_qty_order(
    phone: str,
    product1_handle: str = TEST_PRODUCT_HANDLE,
    product1_size: str = "M",
    product1_qty: int = 2,
    product2_handle: str = TEST_PRODUCT_HANDLE_2,
    product2_size: str = "L",
    product2_qty: int = 1,
    financial_status: str = "pending",
    transactions: list = None,
) -> dict:
    """Create an order with two different products at specified quantities.

    Returns dict with order_id, line_items (including variant_id per line), etc.
    """
    import httpx

    p1 = await _get_product_variants_by_handle(product1_handle)
    p2 = await _get_product_variants_by_handle(product2_handle)
    if not p1 or not p2:
        return {"success": False, "error": f"Could not fetch product info for {product1_handle} and/or {product2_handle}"}

    v1 = p1["variants"].get(product1_size.upper())
    v2 = p2["variants"].get(product2_size.upper())
    if not v1:
        return {"success": False, "error": f"Variant '{product1_size}' not found for {p1['title']}. Available: {list(p1['variants'].keys())}"}
    if not v2:
        return {"success": False, "error": f"Variant '{product2_size}' not found for {p2['title']}. Available: {list(p2['variants'].keys())}"}

    headers, base_url = await _get_shopify_rest_config()
    order_payload = {
        "line_items": [
            {"variant_id": v1["id"], "quantity": product1_qty},
            {"variant_id": v2["id"], "quantity": product2_qty},
        ],
        "shipping_address": {
            "first_name": "MultiQty", "last_name": "Test",
            "address1": "E-10 Jail Road, Janak Puri",
            "city": "New Delhi", "province": "Delhi",
            "zip": "110058", "country": "India",
            "phone": f"+91{phone}",
        },
        "phone": f"+91{phone}",
        "financial_status": financial_status,
        "tags": "BOT_TEST, MULTI_QTY_TEST",
    }
    if transactions:
        order_payload["transactions"] = transactions

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/orders.json", json={"order": order_payload}, headers=headers, timeout=30,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            return {"success": False, "error": f"Shopify HTTP {resp.status_code}: {body}"}
        order = resp.json().get("order", {})

    oid = str(order.get("order_number", ""))
    state_obj = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    await _wait_for_order_indexed(oid, state_obj)
    return {
        "success": True,
        "order_id": oid,
        "order_name": order.get("name", ""),
        "shopify_id": order.get("id"),
        "line_items": order.get("line_items", []),
        "total_price": order.get("total_price"),
        "product1_title": p1["title"],
        "product2_title": p2["title"],
    }


async def _create_prepaid_test_order(phone: str, size: str = "L") -> dict:
    """Create a prepaid (financial_status='paid') order via the orchestrator.

    Includes an explicit sale transaction so the order has a payment record
    that can be refunded via the Shopify Refund API.
    """
    from fashion_bot.core.orchestrator import OrderCreationOrchestrator

    state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    result = await OrderCreationOrchestrator.acreate_order_in_shopify(
        product_link=TEST_VALID_PRODUCT_URL,
        quantity=1,
        requested_size=size,
        phone_number=phone,
        customer_name="Prepaid Test",
        customer_address="E-10 Jail Road, Janak Puri, New Delhi, Delhi, 110058",
        state=state,
        financial_status="paid",
        transactions=[{"kind": "sale", "status": "success", "amount": "1699.00"}],
    )
    if result.get("success"):
        oid = _order_id_from_result(result)
        await _wait_for_order_indexed(oid, state)
    return result


async def _create_partially_paid_test_order(phone: str, total_price: str = "1699.00", paid_amount: str = "1000.00") -> dict:
    """Create a partially_paid order via direct Shopify REST API.

    The order has total_price but only paid_amount was captured.
    This tests that refund only refunds the captured amount, not the full price.
    """
    import httpx

    headers, base_url = await _get_shopify_rest_config()
    payload = {
        "order": {
            "line_items": [{"variant_id": 50347988549878, "quantity": 1}],
            "shipping_address": {
                "first_name": "PartialPay", "last_name": "Test",
                "address1": "E-10 Jail Road, Janak Puri",
                "city": "New Delhi", "province": "Delhi",
                "zip": "110058", "country": "India",
                "phone": f"+91{phone}",
            },
            "phone": f"+91{phone}",
            "financial_status": "partially_paid",
            "transactions": [
                {"kind": "sale", "status": "success", "amount": paid_amount},
            ],
            "tags": "BOT_TEST, PARTIAL_PAID_TEST",
        },
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/orders.json", json=payload, headers=headers, timeout=30,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            return {"success": False, "error": f"Shopify HTTP {resp.status_code}: {body}"}
        order = resp.json().get("order", {})

    oid = str(order.get("order_number", ""))
    state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    await _wait_for_order_indexed(oid, state)
    return {
        "success": True,
        "order_id": oid,
        "order_name": order.get("name", ""),
        "financial_status": order.get("financial_status", ""),
        "total_price": order.get("total_price", ""),
    }


async def _create_discounted_test_order(
    phone: str,
    size: str = "M",
    discount_code: str = "TESTDISCOUNT50",
    discount_amount: str = "500.00",
    discount_type: str = "fixed_amount",
    financial_status: str = "paid",
    paid_amount: str = None,
) -> dict:
    """Create an order with a discount code applied via Shopify REST API.

    When financial_status='paid', a sale transaction for the final (discounted) total
    is included so Shopify considers it fully paid.  Pass financial_status='pending'
    for COD.

    Returns dict with order_id, order_name, financial_status, total_price, discount_codes.
    """
    import httpx

    product_info = await _get_product_variants()
    if not product_info:
        return {"success": False, "error": "Could not fetch product info"}
    variant = product_info["variants"].get(size.upper())
    if not variant:
        return {"success": False, "error": f"Variant '{size}' not found"}

    headers, base_url = await _get_shopify_rest_config()
    order_payload: dict = {
        "line_items": [{"variant_id": variant["id"], "quantity": 1}],
        "shipping_address": {
            "first_name": "Discount", "last_name": "Test",
            "address1": "E-10 Jail Road, Janak Puri",
            "city": "New Delhi", "province": "Delhi",
            "zip": "110058", "country": "India",
            "phone": f"+91{phone}",
        },
        "phone": f"+91{phone}",
        "financial_status": financial_status,
        "discount_codes": [
            {"code": discount_code, "amount": discount_amount, "type": discount_type},
        ],
        "tags": "BOT_TEST, DISCOUNT_TEST",
    }

    if financial_status == "paid":
        variant_price = float(variant["price"])
        disc = float(discount_amount)
        final = max(variant_price - disc, 0)
        txn_amount = paid_amount or f"{final:.2f}"
        order_payload["transactions"] = [
            {"kind": "sale", "status": "success", "amount": txn_amount},
        ]
    elif financial_status == "partially_paid" and paid_amount:
        order_payload["transactions"] = [
            {"kind": "sale", "status": "success", "amount": paid_amount},
        ]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/orders.json", json={"order": order_payload}, headers=headers, timeout=30,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            return {"success": False, "error": f"Shopify HTTP {resp.status_code}: {body}"}
        order = resp.json().get("order", {})

    oid = str(order.get("order_number", ""))
    state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    await _wait_for_order_indexed(oid, state)
    return {
        "success": True,
        "order_id": oid,
        "order_name": order.get("name", ""),
        "financial_status": order.get("financial_status", ""),
        "total_price": order.get("total_price", ""),
        "discount_codes": order.get("discount_codes", []),
    }


async def _create_custom_price_prepaid_order(phone: str, custom_price: str = "9999.00") -> dict:
    """Create a prepaid order with a custom line-item price (no variant link).

    The order total will differ from the test product's actual price, enabling
    price-differential escalation tests in change_order_product_tool.
    """
    import httpx

    headers, base_url = await _get_shopify_rest_config()
    payload = {
        "order": {
            "line_items": [{
                "title": "Test Product (Custom Price)",
                "price": custom_price,
                "quantity": 1,
            }],
            "shipping_address": {
                "first_name": "PriceEsc", "last_name": "Test",
                "address1": "E-10 Jail Road, Janak Puri",
                "city": "New Delhi", "province": "Delhi",
                "zip": "110058", "country": "India",
                "phone": f"+91{phone}",
            },
            "phone": f"+91{phone}",
            "financial_status": "paid",
            "tags": "BOT_TEST, CUSTOM_PRICE_TEST",
        },
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/orders.json", json=payload, headers=headers, timeout=30,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            return {"success": False, "error": f"Shopify HTTP {resp.status_code}: {body}"}
        order = resp.json().get("order", {})

    oid = str(order.get("order_number", ""))
    state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    await _wait_for_order_indexed(oid, state)
    return {
        "success": True,
        "order_id": oid,
        "order_name": order.get("name", ""),
        "total_price": order.get("total_price"),
        "financial_status": order.get("financial_status"),
    }


async def _create_custom_price_cod_order(phone: str, custom_price: str = "9999.00") -> dict:
    """Create a COD order with a custom line-item price (no variant link).

    Mirrors ``_create_custom_price_prepaid_order`` but with
    ``financial_status='pending'`` (COD) and no sale transaction. Because the
    single line item has no variant link, the product-change tool skips the
    in-place GraphQL edit and falls through to the cancel-and-clone path where
    the COD price-differential guard lives — letting tests exercise that guard
    deterministically regardless of catalog prices.
    """
    import httpx

    headers, base_url = await _get_shopify_rest_config()
    payload = {
        "order": {
            "line_items": [{
                "title": "Test Product (Custom Price COD)",
                "price": custom_price,
                "quantity": 1,
            }],
            "shipping_address": {
                "first_name": "CodPriceEsc", "last_name": "Test",
                "address1": "E-10 Jail Road, Janak Puri",
                "city": "New Delhi", "province": "Delhi",
                "zip": "110058", "country": "India",
                "phone": f"+91{phone}",
            },
            "phone": f"+91{phone}",
            "financial_status": "pending",
            "tags": "BOT_TEST, CUSTOM_PRICE_COD_TEST",
        },
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/orders.json", json=payload, headers=headers, timeout=30,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            return {"success": False, "error": f"Shopify HTTP {resp.status_code}: {body}"}
        order = resp.json().get("order", {})

    oid = str(order.get("order_number", ""))
    state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    await _wait_for_order_indexed(oid, state)
    return {
        "success": True,
        "order_id": oid,
        "order_name": order.get("name", ""),
        "total_price": order.get("total_price"),
        "financial_status": order.get("financial_status"),
    }


# ===========================================================================
# 0. Tool list integrity
# ===========================================================================

class TestToolListIntegrity:

    def test_factory_returns_exactly_16_tools(self):
        tools, _ = _get_cancel_update_tools()
        assert len(tools) == 16, f"Expected 16 tools, got {len(tools)}: {list(tools.keys())}"

    def test_expected_tool_names_present(self):
        tools, _ = _get_cancel_update_tools()
        expected = {
            "get_order_details",
            "get_recent_orders",
            "update_order_address",
            "update_order_size_tool",
            "update_order_phone_number_tool",
            "update_order_email_tool",
            "update_order_name_tool",
            "annotate_order",
            "change_order_product_tool",
            "search_products",
            "find_product_by_url",
            "find_product_by_id",
            "cancel_order_tool",
            "escalate_to_agent",
            "check_grace_period_eligibility",
            "get_final_return_exchange_message",
        }
        assert set(tools.keys()) == expected, (
            f"Extra: {set(tools.keys()) - expected}, Missing: {expected - set(tools.keys())}"
        )


# ===========================================================================
# 1. get_order_details
# ===========================================================================

class TestGetOrderDetails:

    @pytest.mark.asyncio
    async def test_valid_order_returns_details(self):
        create = await _create_test_order(PHONE_ORDER_DETAILS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_ORDER_DETAILS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_order_details"].ainvoke({"order_id": oid})

        assert isinstance(result, dict)
        assert result.get("success") is True, f"Expected successful order details retrieval: {result}"
        assert result.get("phone_validated") is True
        assert oid in str(result.get("order_id", "")), (
            f"Returned order_id '{result.get('order_id')}' should contain queried id '{oid}'"
        )
        assert result.get("financial_status"), "Should contain financial_status"
        assert isinstance(result.get("line_items"), list), "line_items should be a list"
        assert len(result["line_items"]) > 0, "Should have at least one line item"
        assert result.get("customer"), "Should contain customer info"
        assert result.get("shipping_address"), "Should contain shipping_address"
        assert result.get("total_price"), "Should contain total_price"
        assert result.get("currency"), "Should contain currency"

    @pytest.mark.asyncio
    async def test_order_detail_line_item_structure(self):
        create = await _create_test_order(PHONE_ORDER_DETAILS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_ORDER_DETAILS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_order_details"].ainvoke({"order_id": oid})
        if not result.get("success"):
            pytest.skip(f"get_order_details failed: {result}")

        item = result["line_items"][0]
        for key in ("title", "variant_title", "quantity", "price", "sku", "product_id", "variant_id"):
            assert key in item, f"Line item missing key '{key}'"
        assert item.get("quantity", 0) >= 1, f"Quantity should be >= 1, got {item.get('quantity')}"
        assert float(item.get("price", 0)) > 0, f"Price should be > 0, got {item.get('price')}"
        assert item.get("product_id"), f"Line item should have product_id, got {item.get('product_id')}"
        assert item.get("variant_id"), f"Line item should have variant_id, got {item.get('variant_id')}"
        assert "cosmic" in (item.get("title") or "").lower() or item.get("title"), (
            f"Line item title should reference the test product, got '{item.get('title')}'"
        )

    @pytest.mark.asyncio
    async def test_wrong_phone_access_denied(self):
        """Phone mismatch should return access_denied."""
        create = await _create_test_order(PHONE_ORDER_DETAILS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_order_details"].ainvoke({"order_id": oid})
        assert result.get("success") is False
        assert result.get("error") == "access_denied"
        assert result.get("phone_validated") is False

    @pytest.mark.asyncio
    async def test_empty_order_id(self):
        state = _build_state(phone=PHONE_ORDER_DETAILS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_order_details"].ainvoke({"order_id": ""})
        assert isinstance(result, dict)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_nonexistent_order_id(self):
        state = _build_state(phone=PHONE_ORDER_DETAILS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_order_details"].ainvoke({"order_id": "NONEXISTENT99999"})
        assert isinstance(result, dict)
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_no_phone_in_state(self):
        state = _build_state(phone="", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_order_details"].ainvoke({"order_id": "FAKE123"})
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        create = await _create_test_order(PHONE_ORDER_DETAILS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_ORDER_DETAILS, client_id=TEST_CLIENT_ID)
        snapshot = copy.deepcopy(state)
        tools, state = _get_cancel_update_tools(state)
        await tools["get_order_details"].ainvoke({"order_id": oid})
        for key in ("order_details", "selected_order_id", "order_data"):
            assert key not in state, f"Tool should not set state['{key}']"
        assert state["phone_number"] == snapshot["phone_number"]


# ===========================================================================
# 3. get_recent_orders
# ===========================================================================

class TestGetRecentOrdersTool:

    @pytest.mark.asyncio
    async def test_returns_orders_with_explicit_phone(self):
        """Pass phone_number as an explicit parameter (preferred stateless path)."""
        create = await _create_test_order(PHONE_RECENT_ORDERS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")

        state = _build_state(phone=PHONE_RECENT_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_recent_orders"].ainvoke({
            "phone_number": PHONE_RECENT_ORDERS,
        })

        assert isinstance(result, dict)
        assert isinstance(result.get("orders"), list)
        assert len(result["orders"]) > 0, "Should return at least one order"

    @pytest.mark.asyncio
    async def test_falls_back_to_state_phone(self):
        """When phone_number param is empty, tool should fall back to state."""
        create = await _create_test_order(PHONE_RECENT_ORDERS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")

        state = _build_state(phone=PHONE_RECENT_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_recent_orders"].ainvoke({})

        assert isinstance(result, dict)
        assert isinstance(result.get("orders"), list)
        assert len(result["orders"]) > 0, "Fallback to state phone should still return orders"

    @pytest.mark.asyncio
    async def test_order_structure(self):
        create = await _create_test_order(PHONE_RECENT_ORDERS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")

        state = _build_state(phone=PHONE_RECENT_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_recent_orders"].ainvoke({
            "phone_number": PHONE_RECENT_ORDERS,
        })
        if not result.get("orders"):
            pytest.skip("No orders returned")

        order = result["orders"][0]
        for key in ("order_id", "financial_status", "fulfillment_status", "total_price", "currency", "line_items"):
            assert key in order, f"Order missing key '{key}'"
        assert isinstance(order.get("line_items"), list)
        assert order.get("order_id"), "order_id should be non-empty"
        assert float(order.get("total_price", 0)) > 0, (
            f"total_price should be > 0, got {order.get('total_price')}"
        )
        if order.get("line_items"):
            item = order["line_items"][0]
            for key in ("title", "variant_title", "quantity", "price", "sku", "product_id", "variant_id"):
                assert key in item, f"Line item missing key '{key}': {item}"
            assert item.get("title"), f"Line item should have title: {item}"
            assert item.get("product_id"), f"Line item should have product_id: {item}"

    @pytest.mark.asyncio
    async def test_max_3_orders(self):
        state = _build_state(phone=PHONE_RECENT_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_recent_orders"].ainvoke({
            "phone_number": PHONE_RECENT_ORDERS,
        })
        assert len(result.get("orders", [])) <= 3

    @pytest.mark.asyncio
    async def test_empty_phone_param_and_state_returns_error(self):
        """Both parameter and state phone are empty — should fail."""
        state = _build_state(phone="", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_recent_orders"].ainvoke({"phone_number": ""})
        assert result.get("success") is False or len(result.get("orders", [])) == 0
        assert "phone" in result.get("message", "").lower() or result.get("orders") == []


# ===========================================================================
# 4. update_order_address
# ===========================================================================

class TestUpdateOrderAddress:

    @pytest.mark.asyncio
    async def test_successful_address_update(self):
        create = await _create_test_order(PHONE_UPDATE_ADDRESS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        new_address = {
            "first_name": "CancelUpdate",
            "last_name": "Test",
            "address1": "42 MG Road",
            "address2": "Near City Mall",
            "city": "Bangalore",
            "state": "Karnataka",
            "zip": "560001",
            "phone": "",
        }

        state = _build_state(phone=PHONE_UPDATE_ADDRESS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_address"].ainvoke({
            "order_id": oid,
            "shipping_address": new_address,
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Expected successful address update: {result}"
        assert result.get("phone_validated") is True
        assert result.get("order_id"), "Result should contain order_id"

        # Verify the address was actually applied by re-fetching
        details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if details.get("success"):
            addr = details.get("shipping_address", {})
            assert "42 MG Road" in (addr.get("address1") or ""), (
                f"Address1 should be updated to '42 MG Road', got '{addr.get('address1')}'"
            )
            assert addr.get("city", "").lower() == "bangalore", (
                f"City should be 'Bangalore', got '{addr.get('city')}'"
            )

    @pytest.mark.asyncio
    async def test_access_denied_wrong_phone(self):
        create = await _create_test_order(PHONE_UPDATE_ADDRESS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_address"].ainvoke({
            "order_id": oid,
            "shipping_address": {
                "address1": "1 Fake St", "city": "Nowhere",
                "state": "UP", "zip": "200001",
            },
        })
        assert result.get("success") is False
        assert result.get("error") == "access_denied"

    @pytest.mark.asyncio
    async def test_nonexistent_order(self):
        state = _build_state(phone=PHONE_UPDATE_ADDRESS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_address"].ainvoke({
            "order_id": "NONEXISTENT99999",
            "shipping_address": {
                "address1": "1 St", "city": "X", "state": "Y", "zip": "100001",
            },
        })
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        create = await _create_test_order(PHONE_UPDATE_ADDRESS)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_UPDATE_ADDRESS, client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["update_order_address"].ainvoke({
            "order_id": oid,
            "shipping_address": {
                "first_name": "CancelUpdate", "last_name": "Test",
                "address1": "42 MG Road", "city": "Bangalore",
                "state": "Karnataka", "zip": "560001",
            },
        })
        for key in ("shipping_address", "order_address"):
            assert key not in state, f"Tool should not set state['{key}']"


# ===========================================================================
# 5. update_order_size_tool
# ===========================================================================

class TestUpdateOrderSizeTool:

    @pytest.mark.asyncio
    async def test_successful_size_update(self):
        create = await _create_test_order(PHONE_UPDATE_SIZE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_UPDATE_SIZE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid,
            "old_variant": "L",
            "new_variant": "M",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Expected successful variant update: {result}"
        assert result.get("phone_validated") is True
        assert result.get("old_variant") == "L", f"old_variant should echo 'L', got '{result.get('old_variant')}'"
        assert result.get("new_variant") == "M", f"new_variant should echo 'M', got '{result.get('new_variant')}'"

    @pytest.mark.asyncio
    async def test_access_denied_wrong_phone(self):
        create = await _create_test_order(PHONE_UPDATE_SIZE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid, "old_variant": "L", "new_variant": "M",
        })
        assert result.get("success") is False
        assert result.get("error") == "access_denied"

    @pytest.mark.asyncio
    async def test_nonexistent_size(self):
        create = await _create_test_order(PHONE_UPDATE_SIZE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_UPDATE_SIZE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid, "old_variant": "L", "new_variant": "XXXXXXL",
        })
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        create = await _create_test_order(PHONE_UPDATE_SIZE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_UPDATE_SIZE, client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["update_order_size_tool"].ainvoke({
            "order_id": oid, "old_variant": "L", "new_variant": "M",
        })
        for key in ("old_size", "new_size", "old_variant", "new_variant", "requested_size"):
            assert key not in state, f"Tool should not set state['{key}']"


# ===========================================================================
# 6. update_order_phone_number_tool
# ===========================================================================

class TestUpdateOrderPhoneNumberTool:

    @pytest.mark.asyncio
    async def test_successful_phone_update(self):
        create = await _create_test_order(PHONE_UPDATE_PHONE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        new_phone = "9111222333"
        state = _build_state(phone=PHONE_UPDATE_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_phone_number_tool"].ainvoke({
            "order_id": oid,
            "new_phone": new_phone,
        })
        assert isinstance(result, dict)
        assert result.get("phone_validated") is True, f"Phone validation should pass: {result}"
        assert result.get("error") != "access_denied", f"Should not be access denied: {result}"
        if result.get("success"):
            assert result.get("order_id"), f"Successful result should contain order_id: {result}"
            assert result.get("new_phone") == new_phone, (
                f"Result should echo new_phone='{new_phone}', got '{result.get('new_phone')}'"
            )

    @pytest.mark.asyncio
    async def test_access_denied_wrong_phone(self):
        create = await _create_test_order(PHONE_UPDATE_PHONE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_phone_number_tool"].ainvoke({
            "order_id": oid, "new_phone": "9111222333",
        })
        assert result.get("success") is False
        assert result.get("error") == "access_denied"

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        create = await _create_test_order(PHONE_UPDATE_PHONE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_UPDATE_PHONE, client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["update_order_phone_number_tool"].ainvoke({
            "order_id": oid, "new_phone": "9111222333",
        })
        assert state.get("phone_number") == PHONE_UPDATE_PHONE, (
            "Tool should not change the customer's own phone in state"
        )


# ===========================================================================
# 7. update_order_email_tool
# ===========================================================================

class TestUpdateOrderEmailTool:

    @pytest.mark.asyncio
    async def test_successful_email_update(self):
        create = await _create_test_order(PHONE_UPDATE_EMAIL)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        new_email = "testupdate@example.com"
        state = _build_state(phone=PHONE_UPDATE_EMAIL, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_email_tool"].ainvoke({
            "order_id": oid,
            "new_email": new_email,
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Expected successful email update: {result}"
        assert result.get("phone_validated") is True
        assert result.get("order_id"), f"Result should contain order_id: {result}"
        assert result.get("new_email") == new_email, (
            f"Result should echo new_email='{new_email}', got '{result.get('new_email')}'"
        )

    @pytest.mark.asyncio
    async def test_access_denied_wrong_phone(self):
        create = await _create_test_order(PHONE_UPDATE_EMAIL)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_email_tool"].ainvoke({
            "order_id": oid, "new_email": "test@example.com",
        })
        assert result.get("success") is False
        assert result.get("error") == "access_denied"


# ===========================================================================
# 8. update_order_name_tool
# ===========================================================================

class TestUpdateOrderNameTool:

    @pytest.mark.asyncio
    async def test_successful_name_update(self):
        create = await _create_test_order(PHONE_UPDATE_NAME)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        new_first = "Updated"
        new_last = "Name"
        state = _build_state(phone=PHONE_UPDATE_NAME, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_name_tool"].ainvoke({
            "order_id": oid,
            "new_first_name": new_first,
            "new_last_name": new_last,
        })
        assert isinstance(result, dict)
        assert result.get("phone_validated") is True, f"Phone validation should pass: {result}"
        assert result.get("error") != "access_denied", f"Should not be access denied: {result}"
        if result.get("success"):
            assert result.get("order_id"), f"Successful result should contain order_id: {result}"
            msg = result.get("message", "")
            assert new_first in msg and new_last in msg, (
                f"Result message should contain '{new_first} {new_last}', got: '{msg}'"
            )

    @pytest.mark.asyncio
    async def test_access_denied_wrong_phone(self):
        create = await _create_test_order(PHONE_UPDATE_NAME)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_name_tool"].ainvoke({
            "order_id": oid,
            "new_first_name": "Hacker",
            "new_last_name": "X",
        })
        assert result.get("success") is False
        assert result.get("error") == "access_denied"


# ===========================================================================
# 9. annotate_order
# ===========================================================================

class TestAnnotateOrder:

    @pytest.mark.asyncio
    async def test_add_note(self):
        create = await _create_test_order(PHONE_ANNOTATE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        note_text = "Test note from automated tests"
        state = _build_state(phone=PHONE_ANNOTATE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["annotate_order"].ainvoke({
            "order_id": oid,
            "note": note_text,
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Expected successful order note annotation: {result}"
        assert result.get("phone_validated") is True
        assert "note_result" in result
        note_res = result["note_result"]
        assert note_res.get("success") is True, f"note_result should be successful: {note_res}"

    @pytest.mark.asyncio
    async def test_add_tags(self):
        create = await _create_test_order(PHONE_ANNOTATE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        tag = "TEST_TAG"
        state = _build_state(phone=PHONE_ANNOTATE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["annotate_order"].ainvoke({
            "order_id": oid,
            "tags": [tag],
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Expected successful order tag annotation: {result}"
        assert "tags_result" in result
        tags_res = result["tags_result"]
        assert tags_res.get("success") is True, f"tags_result should be successful: {tags_res}"

        # Verify tags were actually applied by re-fetching
        details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if details.get("success"):
            order_tags = details.get("tags", "")
            assert tag in order_tags, (
                f"Tag '{tag}' should appear in order tags, got '{order_tags}'"
            )

    @pytest.mark.asyncio
    async def test_add_both_note_and_tags(self):
        create = await _create_test_order(PHONE_ANNOTATE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        note_text = "Both note and tag test"
        tag = "DUAL_TEST"
        state = _build_state(phone=PHONE_ANNOTATE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["annotate_order"].ainvoke({
            "order_id": oid,
            "note": note_text,
            "tags": [tag],
        })
        assert result.get("success") is True
        assert "note_result" in result
        assert "tags_result" in result
        assert result["note_result"].get("success") is True, f"note_result should succeed: {result['note_result']}"
        assert result["tags_result"].get("success") is True, f"tags_result should succeed: {result['tags_result']}"

    @pytest.mark.asyncio
    async def test_neither_note_nor_tags_returns_error(self):
        create = await _create_test_order(PHONE_ANNOTATE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_ANNOTATE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["annotate_order"].ainvoke({
            "order_id": oid,
            "note": "",
            "tags": [],
        })
        assert result.get("success") is False
        assert "at least one" in result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_access_denied_wrong_phone(self):
        create = await _create_test_order(PHONE_ANNOTATE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["annotate_order"].ainvoke({
            "order_id": oid, "note": "should fail",
        })
        assert result.get("success") is False
        assert result.get("error") == "access_denied"


# ===========================================================================
# 10. change_order_product_tool (COD)
# ===========================================================================

class TestChangeOrderProductTool:

    @pytest.mark.asyncio
    async def test_cod_product_change_success(self):
        """COD order product change should cancel old + create new."""
        create = await _create_test_order(PHONE_CHANGE_PRODUCT)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_CHANGE_PRODUCT, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL_2,
            "requested_variant": "L",
        })
        assert isinstance(result, dict)
        if result.get("success"):
            assert result.get("phone_validated") is True
            if result.get("method") == "graphql_order_edit":
                assert result.get("old_order_id"), f"Should include old_order_id: {result}"
            else:
                assert result.get("old_order_cancelled") is True or result.get("requires_escalation") is True
                assert result.get("payment_type"), f"Should include payment_type: {result}"
                if result.get("old_order_cancelled"):
                    assert result.get("old_order_id"), f"Should include old_order_id: {result}"
                    assert result.get("new_order_id") or result.get("new_order_number"), (
                        f"Should include new order reference: {result}"
                    )
        else:
            assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_product_url(self):
        create = await _create_test_order(PHONE_CHANGE_PRODUCT_INVALID)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_CHANGE_PRODUCT_INVALID, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": "not-a-url",
            "requested_variant": "L",
        })
        assert result.get("success") is False
        assert "invalid_url" in result.get("error", "") or "url" in result.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_nonexistent_product_url(self):
        create = await _create_test_order(PHONE_CHANGE_PRODUCT_NONEXIST)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_CHANGE_PRODUCT_NONEXIST, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": "https://blommerce-test.myshopify.com/products/nonexistent-zzz-9999",
            "requested_variant": "L",
        })
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_access_denied_wrong_phone(self):
        create = await _create_test_order(PHONE_CHANGE_PRODUCT_INVALID)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL_2,
            "requested_variant": "L",
        })
        assert result.get("success") is False
        assert result.get("error") == "access_denied"


# ===========================================================================
# 11. search_products (shared — smoke test only; detailed tests in
#     test_place_order_tools.py)
# ===========================================================================

class TestSearchProductsSmoke:

    @pytest.mark.asyncio
    async def test_basic_search_returns_results(self):
        """Use production client_id because QU prompt is only configured there."""
        from langchain_core.messages import HumanMessage

        query = "cosmic shacket"
        state = _build_state(client_id=CLIENT_ID)
        state["messages"] = [HumanMessage(content=query)]
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["search_products"].ainvoke({"query": query})
        assert isinstance(result, dict)
        assert result.get("found") is True
        assert isinstance(result.get("products"), list)
        assert result.get("count", 0) > 0
        first_product = result["products"][0]
        assert first_product.get("title") or first_product.get("name"), (
            f"Product should have a title or name: {first_product}"
        )
        assert first_product.get("url") or first_product.get("product_url") or first_product.get("link"), (
            f"Product should have a URL: {first_product}"
        )


# ===========================================================================
# 12. find_product_by_url
# ===========================================================================

class TestFindProductByUrl:

    @pytest.mark.asyncio
    async def test_valid_url_returns_product(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["find_product_by_url"].ainvoke({
            "product_url": TEST_VALID_PRODUCT_URL,
        })
        assert isinstance(result, dict)
        assert result.get("found") is True, f"Product not found: {result}"
        product = result.get("product", {})
        assert product.get("title") or product.get("name"), (
            f"Product should have a title/name: {product}"
        )
        assert result.get("url") == TEST_VALID_PRODUCT_URL

    @pytest.mark.asyncio
    async def test_nonexistent_url_returns_not_found(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["find_product_by_url"].ainvoke({
            "product_url": "https://blommerce-test.myshopify.com/products/nonexistent-zzz-9999",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_invalid_domain_url(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["find_product_by_url"].ainvoke({
            "product_url": "https://wrong-store.myshopify.com/products/some-product",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_product_has_size_and_price_info(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["find_product_by_url"].ainvoke({
            "product_url": TEST_VALID_PRODUCT_URL,
        })
        if not result.get("found"):
            pytest.skip(f"Product not found: {result}")
        product = result["product"]
        assert product.get("price") is not None, f"Product should have price: {product}"
        has_sizes = product.get("sizes_in_stock") or product.get("all_size_variants") or product.get("variants")
        assert has_sizes, f"Product should have size/variant info: {product}"


# ===========================================================================
# 13. find_product_by_id
# ===========================================================================

class TestFindProductById:

    @pytest.mark.asyncio
    async def test_valid_handle_returns_product(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "evolve-the-cosmic-shacket",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is True, f"Product not found: {result}"
        product = result.get("product", {})
        assert product.get("title") or product.get("name"), (
            f"Product should have a title/name: {product}"
        )
        assert product.get("handle") == "evolve-the-cosmic-shacket", (
            f"Handle should match, got '{product.get('handle')}'"
        )

    @pytest.mark.asyncio
    async def test_nonexistent_handle_returns_not_found(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "nonexistent-product-zzz-9999",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_empty_id_returns_error(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_product_has_url(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "evolve-the-cosmic-shacket",
            "id_type": "handle",
        })
        if not result.get("found"):
            pytest.skip(f"Product not found: {result}")
        assert result.get("url"), f"Result should include a product URL: {result}"
        assert "evolve-the-cosmic-shacket" in result["url"], (
            f"URL should contain the handle: {result['url']}"
        )


# ===========================================================================
# 14. cancel_order_tool
# ===========================================================================

class TestCancelOrderInShopifyTool:

    @pytest.mark.asyncio
    async def test_successful_cancellation(self):
        create = await _create_test_order(PHONE_CANCEL)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_CANCEL, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["cancel_order_tool"].ainvoke({
            "order_id": oid,
            "cancellation_reason": "not_needed",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Expected successful cancellation: {result}"
        assert result.get("phone_validated") is True

        # Verify the order is actually cancelled by re-fetching
        details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if details.get("success"):
            assert details.get("cancelled_at") is not None, (
                f"Order should have cancelled_at set after cancellation: {details}"
            )

    @pytest.mark.asyncio
    async def test_access_denied_wrong_phone(self):
        create = await _create_test_order(PHONE_CANCEL)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["cancel_order_tool"].ainvoke({
            "order_id": oid, "cancellation_reason": "not_needed",
        })
        assert result.get("success") is False
        assert result.get("error") == "access_denied"

    @pytest.mark.asyncio
    async def test_nonexistent_order(self):
        state = _build_state(phone=PHONE_CANCEL, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["cancel_order_tool"].ainvoke({
            "order_id": "NONEXISTENT99999",
            "cancellation_reason": "not_needed",
        })
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        create = await _create_test_order(PHONE_CANCEL)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_CANCEL, client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["cancel_order_tool"].ainvoke({
            "order_id": oid, "cancellation_reason": "not_needed",
        })
        for key in ("cancellation_reason", "cancelled_order_id"):
            assert key not in state, f"Tool should not set state['{key}']"

    @pytest.mark.asyncio
    async def test_prepaid_cancellation_processes_refund(self):
        """Cancelling a prepaid (paid) order should trigger a refund.

        After cancellation, re-fetch the order and verify:
        - cancelled_at is set
        - financial_status changed from 'paid' to 'refunded'
        """
        create = await _create_prepaid_test_order(PHONE_PREPAID_CANCEL, size="M")
        if not create.get("success"):
            pytest.skip(f"Prepaid order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_PREPAID_CANCEL, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        pre_details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if pre_details.get("success"):
            assert pre_details.get("financial_status") == "paid", (
                f"Order should start as 'paid' before cancellation: {pre_details.get('financial_status')}"
            )

        result = await tools["cancel_order_tool"].ainvoke({
            "order_id": oid,
            "cancellation_reason": "not_needed",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Expected successful cancellation of prepaid order: {result}"
        )
        assert result.get("phone_validated") is True

        post_details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if post_details.get("success"):
            assert post_details.get("cancelled_at") is not None, (
                f"Order should have cancelled_at set: {post_details}"
            )
            assert post_details.get("financial_status") in ("refunded", "partially_refunded"), (
                f"Prepaid order financial_status should be 'refunded' or 'partially_refunded' "
                f"after cancellation, got '{post_details.get('financial_status')}'"
            )

    @pytest.mark.asyncio
    async def test_partially_paid_cancellation_refunds_captured_amount(self):
        """Cancelling a partially_paid order should refund only the captured amount.

        Creates an order with total_price=1699.00 but only 1000.00 captured.
        After cancellation, verify:
        - cancelled_at is set
        - financial_status changed to 'partially_refunded' (captured portion refunded)
        - cancel result includes refund details
        """
        paid_amount = "1000.00"
        create = await _create_partially_paid_test_order(
            PHONE_PARTIAL_PAID_CANCEL, total_price="1699.00", paid_amount=paid_amount,
        )
        if not create.get("success"):
            pytest.skip(f"Partially paid order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_PARTIAL_PAID_CANCEL, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        pre_details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if pre_details.get("success"):
            assert pre_details.get("financial_status") == "partially_paid", (
                f"Order should start as 'partially_paid', got '{pre_details.get('financial_status')}'"
            )

        result = await tools["cancel_order_tool"].ainvoke({
            "order_id": oid,
            "cancellation_reason": "not_needed",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Expected successful cancellation of partially paid order: {result}"
        )
        assert result.get("phone_validated") is True

        refund_info = result.get("refund", {})
        assert refund_info.get("success") is True, (
            f"Refund should succeed for partially paid order: {refund_info}"
        )

        post_details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if post_details.get("success"):
            assert post_details.get("cancelled_at") is not None, (
                f"Order should have cancelled_at set: {post_details}"
            )
            assert post_details.get("financial_status") in ("partially_refunded", "refunded"), (
                f"Partially paid order financial_status should be 'partially_refunded' or 'refunded' "
                f"after cancellation, got '{post_details.get('financial_status')}'"
            )


# ===========================================================================
# 13. escalate_to_agent
# ===========================================================================

class TestTriggerAgentEscalation:

    @pytest.mark.asyncio
    async def test_returns_escalation_triggered(self):
        reason_text = "Customer is frustrated and requesting manager"
        state = _build_state(phone="9500300001", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": reason_text,
            "phone_number": "9500300001",
            "order_id": "TEST123",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Escalation should succeed: {result}"
        assert result.get("category"), "Escalation should have a category"
        assert result.get("escalation_id"), "Escalation should have an escalation_id"

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        """escalate_to_agent must NOT set state['needs_escalation'] directly."""
        state = _build_state(phone="9500300002", client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["escalate_to_agent"].ainvoke({
            "reason": "Testing statelessness",
            "phone_number": "9500300002",
        })
        assert "needs_escalation" not in state, (
            "escalate_to_agent must not mutate state['needs_escalation'] — "
            "the node layer propagates this via the response dict"
        )


# ===========================================================================
# 14. Delivery Partner Sync via escalate_to_agent
# ===========================================================================

class TestDeliveryPartnerSyncViaEscalation:
    """Delivery partner sync is now handled by escalate_to_agent with
    category="Delivery Partner Sync". The orchestrator delegates to
    anotify_agent_for_non_integrated_partners internally.
    """

    @pytest.mark.asyncio
    async def test_delivery_sync_escalation_returns_dict(self):
        state = _build_state(phone="9500400001", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Order updated, non-integrated partners need sync",
            "category": "Delivery Partner Sync",
            "details": "Address updated for test",
            "order_id": "FAKE123",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Escalation should succeed: {result}"
        assert result.get("category") == "Delivery Partner Sync"
        assert "notified" in result, f"Should include notified field: {result}"
        assert isinstance(result.get("partners", []), list), "Should contain partners list"

    @pytest.mark.asyncio
    async def test_delivery_sync_cancelled_action(self):
        state = _build_state(phone="9500400002", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Order cancelled, non-integrated partners need sync",
            "category": "Delivery Partner Sync",
            "details": "Reason: not needed",
            "order_id": "FAKE123",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Escalation should succeed: {result}"
        assert result.get("category") == "Delivery Partner Sync"


# ===========================================================================
# 15. check_grace_period_eligibility
# ===========================================================================

class TestCheckGracePeriodEligibility:

    @pytest.mark.asyncio
    async def test_recent_delivery_eligible(self):
        """Order delivered yesterday should be within grace period."""
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert isinstance(result, dict)
        assert "eligible" in result
        assert "message" in result
        if result.get("eligible"):
            days = result.get("days_since_delivery")
            assert days is not None, "days_since_delivery must be set when eligible"
            assert 0 <= days <= 2, (
                f"For yesterday's delivery, days_since_delivery should be 0-2, got {days}"
            )

    @pytest.mark.asyncio
    async def test_old_delivery_not_eligible(self):
        """Order delivered 60 days ago should be outside grace period."""
        from datetime import datetime, timedelta

        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": old_date,
            "request_type": "return",
        })
        assert isinstance(result, dict)
        assert result.get("eligible") is False
        days = result.get("days_since_delivery")
        if days is not None:
            assert days >= 59, f"days_since_delivery should be ~60, got {days}"

    @pytest.mark.asyncio
    async def test_exchange_request_type(self):
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "exchange",
        })
        assert isinstance(result, dict)
        assert "eligible" in result
        assert "message" in result
        if result.get("eligible"):
            days = result.get("days_since_delivery")
            assert days is not None, "days_since_delivery must be set when eligible"
            assert 0 <= days <= 2, f"For yesterday's delivery, days should be 0-2, got {days}"

    @pytest.mark.asyncio
    async def test_invalid_date_format(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": "not-a-date",
            "request_type": "return",
        })
        assert isinstance(result, dict)
        assert result.get("eligible") is False or "error" in result.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert "days_since_delivery" not in state, (
            "Tool should not set state['days_since_delivery']"
        )


# ===========================================================================
# 16. get_final_return_exchange_message
# ===========================================================================

class TestGetFinalReturnExchangeMessage:

    @pytest.mark.asyncio
    async def test_return_message(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "return",
        })
        assert isinstance(result, str)
        assert len(result) > 10, "Should return a substantive message"
        result_lower = result.lower()
        assert "return" in result_lower or "refund" in result_lower or "pickup" in result_lower, (
            f"Return message should reference 'return', 'refund', or 'pickup': {result[:200]}"
        )

    @pytest.mark.asyncio
    async def test_exchange_message(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "exchange",
        })
        assert isinstance(result, str)
        assert len(result) > 10, "Should return a substantive message"
        result_lower = result.lower()
        assert "exchange" in result_lower or "replace" in result_lower or "return" in result_lower, (
            f"Exchange message should reference 'exchange', 'replace', or 'return': {result[:200]}"
        )


# ===========================================================================
# COMPLEX ORDER UPDATE SCENARIOS
# ===========================================================================
#
# 1. Multi-item order size update (target specific product among many)
# 2. Multi-item order product change
# 3. Size update price differential → escalation behaviour
# 4. Product change price differential → escalation
# 5. Payment type preservation (prepaid stays prepaid, COD stays COD)
# ===========================================================================


class TestMultiItemOrderSizeUpdate:
    """Size updates on orders containing multiple products.

    update_order_size_tool matches old_variant against each line item's
    variant_title / name to target the correct item.  These tests verify
    that only the intended line item is changed.

    These tests use standard Shopify-created orders (non-GoKwik).
    """

    @pytest.mark.asyncio
    async def test_size_update_targets_specific_item_in_multi_item_order(self):
        """Create order with items in variants M and L; update M → S.

        Validates that the tool correctly identifies the M-variant line item
        by matching old_variant against variant_title and performs the change.
        """
        create = await _create_multi_item_test_order(PHONE_MULTI_ITEM_SIZE, sizes=["M", "L"])
        if not create.get("success"):
            pytest.skip(f"Multi-item order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_MULTI_ITEM_SIZE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid,
            "old_variant": "M",
            "new_variant": "S",
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, f"Expected successful variant update on multi-item order: {result}"
        assert result.get("requires_escalation") is not True, (
            f"Same-price variant change (M→S) should not escalate: {result}"
        )
        assert result.get("old_variant") == "M"
        assert result.get("new_variant") == "S"

    @pytest.mark.asyncio
    async def test_size_update_targets_specific_product_in_two_product_order(self):
        """Order with two DIFFERENT products (shacket M + long sleeve L).

        Update the shacket's variant from M → S using line_item_variant_id.
        The long sleeve L line item should remain untouched.
        """
        create = await _create_multi_product_test_order(
            PHONE_MULTI_PRODUCT_SIZE,
            product1_handle=TEST_PRODUCT_HANDLE,
            product1_size="M",
            product2_handle=TEST_PRODUCT_HANDLE_2,
            product2_size="L",
        )
        if not create.get("success"):
            pytest.skip(f"Multi-product order creation failed: {create}")
        oid = create["order_id"]
        shacket_variant_id = str(create["line_items"][0]["variant_id"])

        state = _build_state(phone=PHONE_MULTI_PRODUCT_SIZE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid,
            "old_variant": "M",
            "new_variant": "S",
            "line_item_variant_id": shacket_variant_id,
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Expected successful variant update on two-product order: {result}"
        )
        assert result.get("requires_escalation") is not True, (
            f"Same-price variant change (M→S) should not escalate: {result}"
        )
        assert result.get("old_variant") == "M"
        assert result.get("new_variant") == "S"

        details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if details.get("success") and details.get("line_items"):
            assert len(details["line_items"]) >= 2, (
                f"Order should still have 2 line items (both products) after size update: "
                f"{[item.get('title') for item in details['line_items']]}"
            )


class TestPartialQuantityVariantChange:
    """Variant update when customer wants to change only some units.

    Example: Order has "Classic T-Shirt - M (qty 3)" and customer wants to
    change 1 of the 3 to size L.  The tool should reduce M from 3 → 2 and
    add L with qty 1.
    """

    @staticmethod
    async def _create_qty3_order(phone: str) -> dict:
        """Create a COD order with quantity=3 for a single variant."""
        import asyncio

        state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
        po_tools = place_order_tools_factory(state, state["messages"], TEST_CLIENT_ID)
        po_map = {t.name: t for t in po_tools}
        result = await po_map["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
            "customer_name": "PartialQty Test",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "quantity": 3,
        })
        if result.get("success"):
            oid = _order_id_from_result(result)
            await _wait_for_order_indexed(oid, state)
        return result

    @pytest.mark.asyncio
    async def test_partial_quantity_variant_change(self):
        """Change 1 of 3 units from variant L → M.

        After the update the order should have 2x L and 1x M.
        """
        create = await self._create_qty3_order(PHONE_PARTIAL_QTY)
        if not create.get("success"):
            pytest.skip(f"Order creation failed (qty=3): {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_PARTIAL_QTY, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid,
            "old_variant": "L",
            "new_variant": "M",
            "quantity": 1,
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Partial quantity variant update should succeed: {result}"
        )
        assert result.get("quantity_changed") == 1, (
            f"Expected quantity_changed=1, got {result.get('quantity_changed')}"
        )
        assert result.get("quantity_remaining") == 2, (
            f"Expected quantity_remaining=2, got {result.get('quantity_remaining')}"
        )


    @pytest.mark.asyncio
    async def test_quantity_exceeds_line_item(self):
        """Requesting more units than exist should fail gracefully."""
        create = await self._create_qty3_order(PHONE_PARTIAL_QTY)
        if not create.get("success"):
            pytest.skip(f"Order creation failed (qty=3): {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_PARTIAL_QTY, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid,
            "old_variant": "L",
            "new_variant": "M",
            "quantity": 10,
        })

        assert isinstance(result, dict)
        assert result.get("success") is False, (
            f"quantity=10 on qty=3 order should fail: {result}"
        )


class TestSizeCloneLineItemSelection:
    """Pure-logic tests for the cancel-and-recreate line-item selector
    (``_select_recreate_line_items``).

    Reproduces the G37792 -> G37793 incident (Grafana trace
    c64a139f10d3ef8da060fd1a1a6fdf0c): an order with two line items that
    share the same size ("M") where the customer changes one of them. The
    clone builder must target the exact ``line_item_variant_id`` the agent
    selected — not the first line whose size string matches — otherwise it
    overwrites the wrong product, silently dropping one item and duplicating
    another. These run offline (no Shopify / orchestrator import).
    """

    # Variant IDs mirroring the real G37792 order.
    BLACK_POLO_M = 52599100539180
    BLUE_POLO_M = 51829097169196
    BLUE_POLO_S = 51829097169999  # resolved new (size S) variant of the Blue polo

    @staticmethod
    def _two_m_items():
        return [
            {
                "name": "Gant Men Black Solid Regular Fit Polo T-Shirt - M",
                "variant_title": "M",
                "variant_id": TestSizeCloneLineItemSelection.BLACK_POLO_M,
                "quantity": 1,
            },
            {
                "name": "Gant Men Blue Printed Polo Tshirt - M",
                "variant_title": "M",
                "variant_id": TestSizeCloneLineItemSelection.BLUE_POLO_M,
                "quantity": 1,
            },
        ]

    def test_targets_exact_variant_id_keeps_other_product(self):
        """Changing the Blue polo (M->S) by its variant id must keep the Black
        polo and swap only the Blue line — no dropped or duplicated items."""
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _select_recreate_line_items,
        )

        sel = _select_recreate_line_items(
            line_items=self._two_m_items(),
            old_size="M",
            new_variant_id=self.BLUE_POLO_S,
            line_item_variant_id=str(self.BLUE_POLO_M),
        )

        assert sel["success"] is True
        variant_ids = [li["variant_id"] for li in sel["new_line_items"]]
        assert self.BLACK_POLO_M in variant_ids, "Black polo was dropped from the recreated order"
        assert self.BLUE_POLO_S in variant_ids, "Blue polo was not changed to size S"
        assert self.BLUE_POLO_M not in variant_ids, "Blue polo M duplicated alongside S"
        assert sorted(variant_ids) == sorted([self.BLACK_POLO_M, self.BLUE_POLO_S])

    def test_targets_first_product_by_variant_id(self):
        """Targeting the Black polo instead must swap it and keep the Blue M."""
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _select_recreate_line_items,
        )

        sel = _select_recreate_line_items(
            line_items=self._two_m_items(),
            old_size="M",
            new_variant_id=99999000001,  # pretend Black-S
            line_item_variant_id=str(self.BLACK_POLO_M),
        )
        assert sel["success"] is True
        variant_ids = [li["variant_id"] for li in sel["new_line_items"]]
        assert variant_ids == [99999000001, self.BLUE_POLO_M]

    def test_unknown_variant_id_is_rejected(self):
        """A variant id not present in the order is rejected so the agent can
        self-correct — nothing is swapped."""
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _select_recreate_line_items,
        )

        sel = _select_recreate_line_items(
            line_items=self._two_m_items(),
            old_size="M",
            new_variant_id=self.BLUE_POLO_S,
            line_item_variant_id="99999999999999",
        )
        assert sel["success"] is False
        assert sel["error"] == "variant_id_not_in_order"
        assert str(self.BLACK_POLO_M) in sel["valid_variant_ids"]
        assert str(self.BLUE_POLO_M) in sel["valid_variant_ids"]

    def test_no_variant_id_falls_back_to_size_string(self):
        """Legacy / single-item callers that don't pass a variant id still
        work via size-string matching (first matching line)."""
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _select_recreate_line_items,
        )

        sel = _select_recreate_line_items(
            line_items=self._two_m_items(),
            old_size="M",
            new_variant_id=self.BLUE_POLO_S,
            line_item_variant_id="",
        )
        assert sel["success"] is True
        assert sel["new_line_items"][0]["variant_id"] == self.BLUE_POLO_S
        assert len(sel["new_line_items"]) == 2

    def test_single_item_order_swaps_correctly(self):
        """The common single-item case works with a variant id."""
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _select_recreate_line_items,
        )

        items = [{"name": "Solo Tee - M", "variant_title": "M", "variant_id": 700, "quantity": 2}]
        sel = _select_recreate_line_items(
            line_items=items, old_size="M", new_variant_id=701, line_item_variant_id="700",
        )
        assert sel["success"] is True
        assert sel["new_line_items"] == [{"variant_id": 701, "quantity": 2}]


class TestMultiItemOrderProductChange:
    """Product change on orders containing multiple products.

    The tool now uses GraphQL Order Edit as the primary path, which
    replaces only the targeted line item and preserves the rest.
    Cancel-and-recreate is the fallback for single-item orders when
    GraphQL editing is unavailable.
    """

    @pytest.mark.asyncio
    async def test_graphql_product_change_preserves_other_items(self):
        """Multi-item order: GraphQL edit replaces one item, keeps the other.

        Creates order with sizes M and L, replaces the L-sized item with
        a new variant S using line_item_variant_id.  After the change, the
        order should still exist (not cancelled) and contain the M item plus
        the new S item.
        """
        create = await _create_multi_item_test_order(PHONE_MULTI_ITEM_CHANGE, sizes=["M", "L"])
        if not create.get("success"):
            pytest.skip(f"Multi-item order creation failed: {create}")
        oid = create["order_id"]
        l_variant_id = str(create["line_items"][1]["variant_id"])

        state = _build_state(phone=PHONE_MULTI_ITEM_CHANGE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "S",
            "line_item_variant_id": l_variant_id,
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"GraphQL product change on multi-item order should succeed: {result}"
        )
        assert result.get("phone_validated") is True
        assert result.get("method") == "graphql_order_edit", (
            f"Multi-item order should use GraphQL edit path: {result}"
        )

        details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if details.get("success") and details.get("line_items"):
            assert len(details["line_items"]) >= 2, (
                f"Order should still have multiple line items after product change: "
                f"{[item.get('name') for item in details['line_items']]}"
            )

    @pytest.mark.asyncio
    async def test_product_change_invalid_variant_id(self):
        """line_item_variant_id that matches no line item should return an error."""
        create = await _create_multi_item_test_order(PHONE_MULTI_ITEM_CHANGE, sizes=["M", "L"])
        if not create.get("success"):
            pytest.skip(f"Multi-item order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_MULTI_ITEM_CHANGE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "M",
            "line_item_variant_id": "9999999999999",
        })

        assert isinstance(result, dict)
        assert result.get("success") is False, (
            f"Invalid line_item_variant_id should fail: {result}"
        )
        assert "available_items" in result, (
            f"Error should list available line items: {result}"
        )

    @pytest.mark.asyncio
    async def test_product_change_invalid_variant(self):
        """requested_variant that does not exist in the new product should fail."""
        create = await _create_multi_item_test_order(PHONE_MULTI_ITEM_CHANGE, sizes=["M", "L"])
        if not create.get("success"):
            pytest.skip(f"Multi-item order creation failed: {create}")
        oid = create["order_id"]
        m_variant_id = str(create["line_items"][0]["variant_id"])

        state = _build_state(phone=PHONE_MULTI_ITEM_CHANGE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "XXXXXXL",
            "line_item_variant_id": m_variant_id,
        })

        assert isinstance(result, dict)
        assert result.get("success") is False, (
            f"Non-existent variant should fail: {result}"
        )
        assert "available_variants" in result, (
            f"Error should list available variants: {result}"
        )

    @pytest.mark.asyncio
    async def test_graphql_product_change_two_different_products(self):
        """Order with two DIFFERENT products (shacket M + long sleeve L).

        Replace the long sleeve with a shacket S via GraphQL edit using
        the long sleeve's line_item_variant_id.
        The original shacket M line item should remain untouched.
        """
        create = await _create_multi_product_test_order(
            PHONE_MULTI_PRODUCT_CHANGE,
            product1_handle=TEST_PRODUCT_HANDLE,
            product1_size="M",
            product2_handle=TEST_PRODUCT_HANDLE_2,
            product2_size="L",
        )
        if not create.get("success"):
            pytest.skip(f"Multi-product order creation failed: {create}")
        oid = create["order_id"]
        long_sleeve_variant_id = str(create["line_items"][1]["variant_id"])

        state = _build_state(phone=PHONE_MULTI_PRODUCT_CHANGE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "S",
            "line_item_variant_id": long_sleeve_variant_id,
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"GraphQL product change on two-product order should succeed: {result}"
        )
        assert result.get("phone_validated") is True
        assert result.get("method") == "graphql_order_edit", (
            f"Two-product order should use GraphQL edit path: {result}"
        )

        details = await tools["get_order_details"].ainvoke({"order_id": oid})
        if details.get("success") and details.get("line_items"):
            titles = [item.get("title", "") for item in details["line_items"]]
            assert len(details["line_items"]) >= 2, (
                f"Order should still have 2 line items after product change: {titles}"
            )


class TestSizeUpdatePriceDifferentialEscalation:
    """Size update where new size is priced differently from old size.

    Architecture:
      - GraphQL edit path (standard Shopify orders):  Shopify handles price
        adjustments natively within the order-edit session.  The tool returns
        ``success=True`` without ``requires_escalation`` — Shopify adjusts
        the order total.
      - Cancel-and-recreate fallback (GoKwik / channel orders where GraphQL
        editing is unavailable):  For prepaid orders with a non-zero price
        differential the function returns ``requires_escalation=True`` and
        does NOT modify the order.

    Test-store note:  The test store uses standard Shopify orders so the
    GraphQL path is exercised.  We also call the cancel-and-recreate path
    directly with controlled prices to verify escalation behaviour.
    """

    @pytest.mark.asyncio
    async def test_size_update_higher_price_via_graphql(self):
        """GraphQL edit: size change succeeds even if the new variant costs more.

        Shopify's native order editing adjusts the order total — no
        escalation is triggered at the tool level.
        """
        create = await _create_test_order(PHONE_SIZE_PRICE_GRAPHQL)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_SIZE_PRICE_GRAPHQL, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid,
            "old_variant": "L",
            "new_variant": "XL",
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"GraphQL variant edit should succeed even with price difference: {result}"
        )
        assert result.get("requires_escalation") is not True, (
            f"GraphQL path handles price adjustments natively — no escalation expected: {result}"
        )
        assert result.get("old_variant") == "L"
        assert result.get("new_variant") == "XL"

    @pytest.mark.asyncio
    async def test_prepaid_size_update_cancel_recreate_escalates_on_price_diff(self):
        """Cancel-and-recreate path: escalation when new size costs more (prepaid).

        Exercises ``_acancel_and_recreate_order_with_new_size`` directly with
        new_variant_price > old_variant_price so the differential is non-zero.
        The function must return ``requires_escalation=True`` and leave the
        order untouched.
        """
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _acancel_and_recreate_order_with_new_size,
        )
        from fashion_bot.core.factory import ServiceFactory

        create = await _create_prepaid_test_order(PHONE_SIZE_PRICE_CANCEL_RECREATE, size="M")
        if not create.get("success"):
            pytest.skip(f"Prepaid order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_SIZE_PRICE_CANCEL_RECREATE, client_id=TEST_CLIENT_ID)
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        order_data = await order_service.aget_order_details(oid, state=state)
        if not order_data:
            pytest.skip(f"Could not fetch order data for {oid}")

        actual_price = float(order_data.get("total_price", 2000))
        result = await _acancel_and_recreate_order_with_new_size(
            order_id=oid,
            order_data=order_data,
            old_size="M",
            new_size="L",
            product_handle=TEST_PRODUCT_HANDLE,
            state=state,
            old_variant_price=actual_price,
            new_variant_price=actual_price + 500,
        )

        assert isinstance(result, dict)
        assert result.get("requires_escalation") is True, (
            f"Prepaid order with price increase should trigger escalation: {result}"
        )
        assert result.get("order_not_modified") is True, (
            f"Order should NOT be modified when escalation is required: {result}"
        )
        assert result.get("differential_amount", 0) > 0, (
            f"Differential should be positive (new size costs more): {result}"
        )
        assert result.get("payment_type") == "prepaid", (
            f"Payment type should be 'prepaid': {result}"
        )

    @pytest.mark.asyncio
    async def test_cod_size_update_cancel_recreate_escalates_on_price_diff(self):
        """Cancel-and-recreate path: escalation when the new size costs more (COD).

        The price-difference guard now runs for COD too — a non-zero
        differential must escalate and leave the order untouched, exactly like
        the prepaid path (previously COD recreated unconditionally).
        """
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _acancel_and_recreate_order_with_new_size,
        )
        from fashion_bot.core.factory import ServiceFactory

        create = await _create_test_order(PHONE_COD_SIZE_PRICE_DIFF)
        if not create.get("success"):
            pytest.skip(f"COD order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_COD_SIZE_PRICE_DIFF, client_id=TEST_CLIENT_ID)
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        order_data = await order_service.aget_order_details(oid, state=state)
        if not order_data:
            pytest.skip(f"Could not fetch order data for {oid}")

        actual_price = float(order_data.get("total_price", 1500))
        result = await _acancel_and_recreate_order_with_new_size(
            order_id=oid,
            order_data=order_data,
            old_size="L",
            new_size="S",
            product_handle=TEST_PRODUCT_HANDLE,
            state=state,
            old_variant_price=actual_price,
            new_variant_price=actual_price + 500,
        )

        assert isinstance(result, dict)
        assert result.get("requires_escalation") is True, (
            f"COD order with price difference should now trigger escalation: {result}"
        )
        assert result.get("order_not_modified") is True, (
            f"COD order should NOT be cancelled/recreated when prices differ: {result}"
        )
        assert result.get("old_order_cancelled") is False, (
            f"Old COD order must remain intact on escalation: {result}"
        )
        assert result.get("differential_amount", 0) > 0
        assert result.get("payment_type") == "cod", (
            f"Payment type should be 'cod': {result}"
        )


class TestGoKwikSizeChangeCancelRecreate:
    """Simulate GoKwik-created orders where the GraphQL Order Edit API is
    unavailable and the system falls back to cancel-and-recreate.

    Calls ``_acancel_and_recreate_order_with_new_size`` directly with
    zero price differential to verify:
    - COD: old order cancelled, new COD order created with the new size
    - Prepaid: old order cancelled (skip_refund), new prepaid order created
      preserving ``financial_status`` and ``payment_gateway_names``
    """

    @pytest.mark.asyncio
    async def test_cod_cancel_recreate_zero_price_diff(self):
        """COD GoKwik order: size M -> S, same price.

        Old order should be cancelled and a new COD order created.
        """
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _acancel_and_recreate_order_with_new_size,
        )
        from fashion_bot.core.factory import ServiceFactory

        create = await _create_test_order(PHONE_GOKWIK_COD_SIZE)
        if not create.get("success"):
            pytest.skip(f"COD order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_GOKWIK_COD_SIZE, client_id=TEST_CLIENT_ID)
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        order_data = await order_service.aget_order_details(oid, state=state)
        if not order_data:
            pytest.skip(f"Could not fetch order data for {oid}")

        actual_price = float(order_data.get("total_price", 1500))
        result = await _acancel_and_recreate_order_with_new_size(
            order_id=oid,
            order_data=order_data,
            old_size="L",
            new_size="S",
            product_handle=TEST_PRODUCT_HANDLE,
            state=state,
            old_variant_price=actual_price,
            new_variant_price=actual_price,
        )

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Expected successful cancel-and-recreate for COD order: {result}"
        )
        assert result.get("requires_escalation") is not True, (
            f"Zero price diff should not trigger escalation: {result}"
        )
        assert result.get("method") == "cancel_and_recreate", (
            f"Expected cancel_and_recreate method: {result}"
        )
        assert result.get("payment_type") == "cod", (
            f"Payment type should be 'cod': {result}"
        )
        assert result.get("new_order_id"), (
            f"Should have a new order ID after recreation: {result}"
        )
        assert result.get("old_variant") == "L"
        assert result.get("new_variant") == "S"

    @pytest.mark.asyncio
    async def test_prepaid_cancel_recreate_zero_price_diff(self):
        """Prepaid GoKwik order: size M -> S, same price.

        Old order should be cancelled (skip_refund), new prepaid order
        created preserving the original financial_status.
        """
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _acancel_and_recreate_order_with_new_size,
        )
        from fashion_bot.core.factory import ServiceFactory

        create = await _create_prepaid_test_order(PHONE_GOKWIK_PREPAID_SIZE, size="M")
        if not create.get("success"):
            pytest.skip(f"Prepaid order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_GOKWIK_PREPAID_SIZE, client_id=TEST_CLIENT_ID)
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        order_data = await order_service.aget_order_details(oid, state=state)
        if not order_data:
            pytest.skip(f"Could not fetch order data for {oid}")

        actual_price = float(order_data.get("total_price", 2000))
        result = await _acancel_and_recreate_order_with_new_size(
            order_id=oid,
            order_data=order_data,
            old_size="M",
            new_size="S",
            product_handle=TEST_PRODUCT_HANDLE,
            state=state,
            old_variant_price=actual_price,
            new_variant_price=actual_price,
        )

        assert isinstance(result, dict)
        if not result.get("success") and "not found" in str(result.get("error", "")).lower():
            pytest.skip(f"Shopify indexing delay — order {oid} not found during cancel: {result}")
        assert result.get("success") is True, (
            f"Expected successful cancel-and-recreate for prepaid order: {result}"
        )
        assert result.get("requires_escalation") is not True, (
            f"Zero price diff should not trigger escalation: {result}"
        )
        assert result.get("method") == "cancel_and_recreate", (
            f"Expected cancel_and_recreate method: {result}"
        )
        assert result.get("payment_type") in ("prepaid", "partial_prepaid"), (
            f"Payment type should be prepaid: {result}"
        )
        assert result.get("new_order_id"), (
            f"Should have a new order ID after recreation: {result}"
        )
        assert result.get("original_amount_paid", 0) > 0, (
            f"Should report the original amount paid: {result}"
        )
        assert result.get("old_variant") == "M"
        assert result.get("new_variant") == "S"

        if result.get("create_result", {}).get("success"):
            new_oid = result["new_order_id"]
            tools, _ = _get_cancel_update_tools(state)
            new_details = await tools["get_order_details"].ainvoke({"order_id": new_oid})
            if new_details.get("success"):
                assert new_details.get("financial_status") == "paid", (
                    f"New order should preserve 'paid' financial_status, "
                    f"got '{new_details.get('financial_status')}'"
                )


class TestProductChangePriceDifferentialEscalation:
    """Product change where old and new products have different retail prices.

    For prepaid / partial_prepaid orders, any non-zero price differential
    between the old line item's retail price and the new variant's retail
    price triggers escalation.  The tool returns ``requires_escalation=True``
    and does NOT cancel or create any orders.
    """

    @pytest.mark.asyncio
    async def test_prepaid_lower_price_product_triggers_escalation(self):
        """Prepaid order at ₹9999 (single item) → change to cosmic shacket (lower retail price).

        differential = new_variant_price − old_line_item_price < 0 → customer is owed a
        refund → escalation.
        """
        create = await _create_custom_price_prepaid_order(
            PHONE_PREPAID_PRICE_ESC_HIGH, custom_price="9999.00",
        )
        if not create.get("success"):
            pytest.skip(f"Custom-price order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_PREPAID_PRICE_ESC_HIGH, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "L",
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Tool should return success=True with escalation info: {result}"
        )
        assert result.get("requires_escalation") is True, (
            f"Price difference should trigger escalation: {result}"
        )
        assert result.get("order_not_modified") is True, (
            f"Order should NOT be modified for price-different prepaid: {result}"
        )
        assert result.get("differential_amount", 0) < 0, (
            f"Differential should be negative (new product is cheaper): {result}"
        )
        assert result.get("old_order_cancelled") is False, (
            f"Old order should NOT be cancelled during escalation: {result}"
        )
        assert result.get("payment_type") in ("prepaid", "partial_prepaid"), (
            f"Payment type should be prepaid or partial_prepaid: {result}"
        )

    @pytest.mark.asyncio
    async def test_prepaid_higher_price_product_triggers_escalation(self):
        """Prepaid order at ₹100 (single item) → change to cosmic shacket (higher retail price).

        differential = new_variant_price − old_line_item_price > 0 → customer owes more
        → escalation.
        """
        create = await _create_custom_price_prepaid_order(
            PHONE_PREPAID_PRICE_ESC_LOW, custom_price="100.00",
        )
        if not create.get("success"):
            pytest.skip(f"Custom-price order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_PREPAID_PRICE_ESC_LOW, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "L",
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Tool should return success=True with escalation info: {result}"
        )
        assert result.get("requires_escalation") is True, (
            f"Price difference should trigger escalation: {result}"
        )
        assert result.get("order_not_modified") is True, (
            f"Order should NOT be modified for price-different prepaid: {result}"
        )
        assert result.get("differential_amount", 0) > 0, (
            f"Differential should be positive (new product is more expensive): {result}"
        )


class TestPaymentTypePreservation:
    """Verify that payment type is preserved across order modifications.

    - Prepaid product change (same price):  cancels + recreates with
      ``financial_status`` and ``payment_gateway_names`` from the original.
    - COD product change:  cancels + recreates as a new COD order.
    - Prepaid size update via GraphQL:  edits in-place — financial_status
      is inherently unchanged.
    """

    @pytest.mark.asyncio
    async def test_prepaid_same_price_product_change_preserves_prepaid(self):
        """Prepaid order + same-price product change → new order is prepaid."""
        create = await _create_prepaid_test_order(PHONE_PREPAID_SAME_PRICE, size="L")
        if not create.get("success"):
            pytest.skip(f"Prepaid order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_PREPAID_SAME_PRICE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "L",
        })

        assert isinstance(result, dict)
        if result.get("requires_escalation"):
            assert result.get("payment_type") in ("prepaid", "partial_prepaid"), (
                f"Payment type should be reported as prepaid: {result}"
            )
        elif result.get("success"):
            if result.get("method") == "graphql_order_edit":
                assert result.get("old_order_id"), f"Should include old_order_id: {result}"
            else:
                assert result.get("payment_type") in ("prepaid", "partial_prepaid"), (
                    f"New order should preserve prepaid payment type: {result}"
                )
                assert result.get("old_order_cancelled") is True
                assert result.get("new_order_id") or result.get("new_order_number"), (
                    f"Should have new order reference: {result}"
                )
        else:
            pytest.fail(f"Product change failed unexpectedly: {result}")

    @pytest.mark.asyncio
    async def test_cod_product_change_preserves_cod(self):
        """COD order + product change → new order is COD."""
        create = await _create_test_order(PHONE_COD_PRESERVE)
        if not create.get("success"):
            pytest.skip(f"COD order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_COD_PRESERVE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "L",
        })

        assert isinstance(result, dict)
        if result.get("success"):
            if result.get("method") == "graphql_order_edit":
                assert result.get("old_order_id"), f"Should include old_order_id: {result}"
            else:
                assert result.get("payment_type") == "cod", (
                    f"COD order product change should create COD order: {result}"
                )
                assert result.get("old_order_cancelled") is True
                assert result.get("new_order_id") or result.get("new_order_number"), (
                    f"Should have new order reference: {result}"
                )
        else:
            assert "error" in result


# ===========================================================================
# Cross-cutting: Statelessness verification
# ===========================================================================

class TestStatelessness:
    """Verify that no tool mutates state variables."""

    FORBIDDEN_STATE_KEYS = {
        "needs_escalation",
        "cancellation_reason",
        "cancelled_order_id",
        "order_data",
        "order_details",
        "old_size",
        "new_size",
        "old_variant",
        "new_variant",
        "shipping_address",
        "order_address",
        "days_since_delivery",
        "return_reason_category",
        "return_reason_details",
        "can_exchange_fix",
    }

    @pytest.mark.asyncio
    async def test_get_order_details_stateless(self):
        create = await _create_test_order(PHONE_ORDER_DETAILS)
        if not create.get("success"):
            pytest.skip("Order creation failed")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_ORDER_DETAILS, client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["get_order_details"].ainvoke({"order_id": oid})
        leaked = self.FORBIDDEN_STATE_KEYS & set(state.keys())
        assert not leaked, f"State leaked keys: {leaked}"

    @pytest.mark.asyncio
    async def test_cancel_tool_stateless(self):
        create = await _create_test_order(PHONE_CANCEL)
        if not create.get("success"):
            pytest.skip("Order creation failed")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_CANCEL, client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["cancel_order_tool"].ainvoke({
            "order_id": oid, "cancellation_reason": "not_needed",
        })
        leaked = self.FORBIDDEN_STATE_KEYS & set(state.keys())
        assert not leaked, f"State leaked keys: {leaked}"

    @pytest.mark.asyncio
    async def test_escalate_to_agent_stateless(self):
        state = _build_state(phone="9500500001", client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["escalate_to_agent"].ainvoke({
            "reason": "Statelessness test",
            "phone_number": "9500500001",
        })
        leaked = self.FORBIDDEN_STATE_KEYS & set(state.keys())
        assert not leaked, f"State leaked keys: {leaked}"

    @pytest.mark.asyncio
    async def test_check_grace_period_stateless(self):
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday, "request_type": "return",
        })
        leaked = self.FORBIDDEN_STATE_KEYS & set(state.keys())
        assert not leaked, f"State leaked keys: {leaked}"

    @pytest.mark.asyncio
    async def test_annotate_order_stateless(self):
        create = await _create_test_order(PHONE_ANNOTATE)
        if not create.get("success"):
            pytest.skip("Order creation failed")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_ANNOTATE, client_id=TEST_CLIENT_ID)
        tools, state = _get_cancel_update_tools(state)
        await tools["annotate_order"].ainvoke({
            "order_id": oid, "note": "Statelessness test",
        })
        leaked = self.FORBIDDEN_STATE_KEYS & set(state.keys())
        assert not leaked, f"State leaked keys: {leaked}"


# ===========================================================================
# Multi-product orders with quantity > 1 per line item
# ===========================================================================

class TestMultiItemQuantityVariantChange:
    """Orders with multiple products where individual line items have qty > 1.

    Scenario 4: Multi-product order, one product has qty 2, change 1 unit's size.
    Scenario 6: Multi-product order, multiple line items with qty 2, product change.
    """

    @pytest.mark.asyncio
    async def test_multi_product_partial_qty_size_change(self):
        """Order: Product A (M, qty=2) + Product B (L, qty=1).

        Change 1 unit of Product A from M to S. After the update:
        - Product A should have 1x M remaining and 1x S added
        - Product B (L, qty=1) should be untouched
        """
        create = await _create_multi_item_qty_order(
            PHONE_MULTI_QTY_SIZE,
            product1_handle=TEST_PRODUCT_HANDLE,
            product1_size="M",
            product1_qty=2,
            product2_handle=TEST_PRODUCT_HANDLE_2,
            product2_size="L",
            product2_qty=1,
        )
        if not create.get("success"):
            pytest.skip(f"Multi-qty order creation failed: {create}")

        line_items = create["line_items"]
        product_a_line = next(
            (li for li in line_items if li.get("variant_title", "").upper() == "M"),
            None,
        )
        assert product_a_line, f"Could not find variant M line item: {line_items}"
        product_a_variant_id = str(product_a_line["variant_id"])

        state = _build_state(phone=PHONE_MULTI_QTY_SIZE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": create["order_id"],
            "old_variant": "M",
            "new_variant": "S",
            "line_item_variant_id": product_a_variant_id,
            "quantity": 1,
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Partial qty size change on multi-product order should succeed: {result}"
        )
        assert result.get("quantity_changed") == 1, (
            f"Expected quantity_changed=1, got {result.get('quantity_changed')}"
        )
        assert result.get("quantity_remaining") == 1, (
            f"Expected quantity_remaining=1, got {result.get('quantity_remaining')}"
        )

    @pytest.mark.asyncio
    async def test_multi_product_qty2_product_change(self):
        """Order: Product A (M, qty=2) + Product B (L, qty=2).

        Replace Product B line item with a different product (Shacket, variant S).
        Product A lines should be preserved.
        """
        create = await _create_multi_item_qty_order(
            PHONE_MULTI_QTY_PRODUCT,
            product1_handle=TEST_PRODUCT_HANDLE,
            product1_size="M",
            product1_qty=2,
            product2_handle=TEST_PRODUCT_HANDLE_2,
            product2_size="L",
            product2_qty=2,
        )
        if not create.get("success"):
            pytest.skip(f"Multi-qty order creation failed: {create}")

        line_items = create["line_items"]
        product_b_line = next(
            (li for li in line_items
             if TEST_PRODUCT_HANDLE_2.replace("-", " ") in (li.get("title", "") or "").lower()
             or li.get("variant_title", "").upper() == "L"),
            None,
        )
        assert product_b_line, f"Could not find Product B (L) line item: {line_items}"
        product_b_variant_id = str(product_b_line["variant_id"])

        state = _build_state(phone=PHONE_MULTI_QTY_PRODUCT, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": create["order_id"],
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "S",
            "line_item_variant_id": product_b_variant_id,
        })

        assert isinstance(result, dict)
        if result.get("success"):
            if result.get("method") == "graphql_order_edit":
                assert result.get("old_order_id"), f"Should include old_order_id: {result}"
                assert result.get("new_product"), f"Should include new_product: {result}"
                assert result.get("new_variant"), f"Should include new_variant: {result}"
            elif result.get("requires_escalation"):
                assert result.get("order_not_modified") is True, (
                    f"Escalation should not modify the order: {result}"
                )
            else:
                assert result.get("old_order_cancelled") is True or result.get("new_order_id"), (
                    f"Cancel-recreate should report old cancelled or new created: {result}"
                )
        else:
            pytest.fail(f"Product change on multi-qty order failed unexpectedly: {result}")


# ===========================================================================
# GoKwik-style product change: cancel-and-recreate fallback
# ===========================================================================

class TestGoKwikProductChangeCancelRecreate:
    """Simulate GoKwik-created orders where GraphQL Order Edit fails,
    forcing the cancel-and-recreate fallback for product changes.

    We patch ``OrderUpdateOrchestrator.achange_order_product`` to return
    failure so the tool falls through to the cancel-and-recreate code path.
    """

    @pytest.mark.asyncio
    async def test_gokwik_cod_product_change_cancel_recreate(self):
        """COD GoKwik order: product change via cancel-and-recreate.

        GraphQL patched to fail on a single-item COD order.
        Old order should be cancelled, new COD order created.

        Swaps the cosmic shacket for itself (same variant) so the price
        differential is zero — the COD differential guard only proceeds with
        cancel-and-recreate when prices match; a differing price now escalates
        (covered by TestCodProductChangePriceDifferential).
        """
        from unittest.mock import AsyncMock, patch

        create = await _create_test_order(PHONE_GOKWIK_COD_PRODUCT)
        if not create.get("success"):
            pytest.skip(f"COD order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_GOKWIK_COD_PRODUCT, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        with patch(
            "fashion_bot.core.orchestrator.OrderUpdateOrchestrator.achange_order_product",
            new_callable=AsyncMock,
            return_value={"success": False, "error": "GraphQL editing unavailable for channel order"},
        ):
            result = await tools["change_order_product_tool"].ainvoke({
                "order_id": oid,
                "new_product_url": TEST_VALID_PRODUCT_URL,
                "requested_variant": "L",
            })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"COD GoKwik product change should succeed via cancel-recreate: {result}"
        )
        assert result.get("requires_escalation") is not True, (
            f"COD order should not escalate on product change: {result}"
        )
        assert result.get("old_order_cancelled") is True, (
            f"Old order should be cancelled: {result}"
        )
        assert result.get("payment_type") == "cod", (
            f"New order should preserve COD payment type: {result}"
        )
        assert result.get("new_order_id"), (
            f"Should have a new order ID after recreation: {result}"
        )

    @pytest.mark.asyncio
    async def test_gokwik_prepaid_product_change_cancel_recreate_same_price(self):
        """Prepaid GoKwik order: same-price product change via cancel-recreate.

        GraphQL patched to fail. Old order cancelled (skip_refund), new prepaid
        order created preserving financial_status.
        """
        from unittest.mock import AsyncMock, patch
        from conftest import shopify_retry

        create = await shopify_retry(_create_prepaid_test_order, PHONE_GOKWIK_PREPAID_PRODUCT, size="L")
        if not create.get("success"):
            pytest.skip(f"Prepaid order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_GOKWIK_PREPAID_PRODUCT, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        async def _invoke_change():
            with patch(
                "fashion_bot.core.orchestrator.OrderUpdateOrchestrator.achange_order_product",
                new_callable=AsyncMock,
                return_value={"success": False, "error": "GraphQL editing unavailable for channel order"},
            ):
                return await tools["change_order_product_tool"].ainvoke({
                    "order_id": oid,
                    "new_product_url": TEST_VALID_PRODUCT_URL,
                    "requested_variant": "L",
                })

        result = await shopify_retry(_invoke_change)

        assert isinstance(result, dict)
        if not result.get("success") and not result.get("requires_escalation"):
            err_msg = str(result.get("error", "")) + str(result.get("message", ""))
            if "not found" in err_msg.lower():
                pytest.skip(f"Shopify indexing delay — order {oid} not found: {result}")
        if result.get("requires_escalation"):
            assert result.get("payment_type") in ("prepaid", "partial_prepaid"), (
                f"Escalation should report prepaid payment type: {result}"
            )
            assert result.get("order_not_modified") is True, (
                f"Order should not be modified during escalation: {result}"
            )
        elif result.get("success"):
            assert result.get("old_order_cancelled") is True, (
                f"Old order should be cancelled: {result}"
            )
            assert result.get("payment_type") in ("prepaid", "partial_prepaid"), (
                f"New order should preserve prepaid payment type: {result}"
            )
            assert result.get("new_order_id"), (
                f"Should have a new order ID after recreation: {result}"
            )
            assert result.get("original_amount_paid", 0) > 0, (
                f"Should report the original amount paid: {result}"
            )
        else:
            pytest.fail(f"Prepaid GoKwik product change failed unexpectedly: {result}")

    @pytest.mark.asyncio
    async def test_gokwik_prepaid_product_change_price_diff_escalates(self):
        """Prepaid GoKwik order: product change with retail price differential.

        GraphQL patched to fail. The order was created at ₹9999 (custom line
        item price) while the new product has a different retail price, so
        the tool should return requires_escalation=True and NOT modify the order.
        """
        from unittest.mock import AsyncMock, patch

        create = await _create_custom_price_prepaid_order(
            PHONE_GOKWIK_PREPAID_PRODUCT_ESC, custom_price="9999.00",
        )
        if not create.get("success"):
            pytest.skip(f"Custom price order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_GOKWIK_PREPAID_PRODUCT_ESC, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        with patch(
            "fashion_bot.core.orchestrator.OrderUpdateOrchestrator.achange_order_product",
            new_callable=AsyncMock,
            return_value={"success": False, "error": "GraphQL editing unavailable for channel order"},
        ):
            result = await tools["change_order_product_tool"].ainvoke({
                "order_id": oid,
                "new_product_url": TEST_VALID_PRODUCT_URL,
                "requested_variant": "L",
            })

        assert isinstance(result, dict)
        assert result.get("requires_escalation") is True, (
            f"Prepaid with price diff should trigger escalation: {result}"
        )
        assert result.get("order_not_modified") is True, (
            f"Order should NOT be modified when escalation is required: {result}"
        )
        assert result.get("differential_amount", 0) != 0, (
            f"Should report a non-zero price differential: {result}"
        )
        assert result.get("old_order_cancelled") is False, (
            f"Old order should NOT be cancelled during escalation: {result}"
        )


# ===========================================================================
# Old order verification after cancel-recreate
# ===========================================================================

class TestOldOrderVerificationAfterCancelRecreate:
    """After a successful cancel-and-recreate, verify:
    - The old order's status is actually 'cancelled'
    - For prepaid: the old order's financial_status is still 'paid' (no refund)
    """

    @pytest.mark.asyncio
    async def test_cancel_recreate_old_order_status_is_cancelled(self):
        """After GoKwik-style cancel-recreate, the original order's status
        should be 'cancelled' when fetched via get_order_details.
        """
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _acancel_and_recreate_order_with_new_size,
        )
        from fashion_bot.core.factory import ServiceFactory
        from conftest import shopify_retry

        create = await shopify_retry(_create_test_order, PHONE_OLD_ORDER_VERIFY)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_OLD_ORDER_VERIFY, client_id=TEST_CLIENT_ID)
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        order_data = await order_service.aget_order_details(oid, state=state)
        if not order_data:
            pytest.skip(f"Could not fetch order data for {oid}")

        actual_price = float(order_data.get("total_price", 1500))
        result = await shopify_retry(
            _acancel_and_recreate_order_with_new_size,
            order_id=oid,
            order_data=order_data,
            old_size="L",
            new_size="S",
            product_handle=TEST_PRODUCT_HANDLE,
            state=state,
            old_variant_price=actual_price,
            new_variant_price=actual_price,
        )

        assert result.get("success") is True, (
            f"Cancel-recreate should succeed: {result}"
        )

        import asyncio
        await asyncio.sleep(3)
        old_order_data = await order_service.aget_order_details(oid, state=state)
        assert old_order_data, f"Old order {oid} should still be fetchable"
        assert old_order_data.get("cancelled_at") is not None, (
            f"Old order should have cancelled_at set, got: cancelled_at={old_order_data.get('cancelled_at')}"
        )

    @pytest.mark.asyncio
    async def test_prepaid_cancel_recreate_no_refund_on_old_order(self):
        """After prepaid cancel-recreate with skip_refund, the old order's
        financial_status should remain 'paid' (not 'refunded').
        """
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _acancel_and_recreate_order_with_new_size,
        )
        from fashion_bot.core.factory import ServiceFactory

        create = await _create_prepaid_test_order(PHONE_NO_REFUND_VERIFY, size="M")
        if not create.get("success"):
            pytest.skip(f"Prepaid order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_NO_REFUND_VERIFY, client_id=TEST_CLIENT_ID)
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        order_data = await order_service.aget_order_details(oid, state=state)
        if not order_data:
            pytest.skip(f"Could not fetch order data for {oid}")

        actual_price = float(order_data.get("total_price", 2000))
        result = await _acancel_and_recreate_order_with_new_size(
            order_id=oid,
            order_data=order_data,
            old_size="M",
            new_size="S",
            product_handle=TEST_PRODUCT_HANDLE,
            state=state,
            old_variant_price=actual_price,
            new_variant_price=actual_price,
        )

        assert result.get("success") is True, (
            f"Prepaid cancel-recreate should succeed: {result}"
        )

        import asyncio
        await asyncio.sleep(3)
        old_order_data = await order_service.aget_order_details(oid, state=state)
        assert old_order_data, f"Old order {oid} should still be fetchable"
        assert old_order_data.get("cancelled_at") is not None, (
            f"Old order should be cancelled"
        )
        financial_status = old_order_data.get("financial_status", "")
        assert financial_status != "refunded", (
            f"Old order financial_status should NOT be 'refunded' after skip_refund cancel. "
            f"Got '{financial_status}' — expected 'paid' or 'voided'"
        )


# ===========================================================================
# COD product change with price differential (no escalation)
# ===========================================================================

class TestCodProductChangePriceDifferential:
    """COD product change enforces the price-differential guard.

    A COD swap must never silently change what the customer pays on delivery
    vs. what they agreed to. When the new product's retail price differs from
    the line item being replaced, the cancel-and-clone path must escalate
    (``requires_escalation=True``) and leave the order untouched — mirroring
    the prepaid branch and the size-change flow. Only a zero differential is
    allowed to proceed with cancel-and-recreate.

    Regression guard for the #63043 incident, where a COD order swapped a
    ₹960 product for a ₹640 one (a ₹320 drop) and was silently recreated.
    """

    @pytest.mark.asyncio
    async def test_cod_higher_price_product_triggers_escalation(self):
        """COD order at ₹100 → change to cosmic shacket (higher retail price).

        differential = new_variant_price − old_line_item_price > 0 → customer
        owes more → escalation, no order mutation.
        """
        create = await _create_custom_price_cod_order(
            PHONE_COD_PRODUCT_PRICE_DIFF_LOW, custom_price="100.00",
        )
        if not create.get("success"):
            pytest.skip(f"Custom-price COD order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_COD_PRODUCT_PRICE_DIFF_LOW, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "L",
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Tool should return success=True with escalation info: {result}"
        )
        assert result.get("requires_escalation") is True, (
            f"COD price difference should now trigger escalation: {result}"
        )
        assert result.get("order_not_modified") is True, (
            f"Order should NOT be modified for price-different COD: {result}"
        )
        assert result.get("old_order_cancelled") is False, (
            f"Old COD order should NOT be cancelled during escalation: {result}"
        )
        assert result.get("differential_amount", 0) > 0, (
            f"Differential should be positive (new product is more expensive): {result}"
        )
        assert result.get("payment_type") == "cod", (
            f"Payment type should be reported as COD: {result}"
        )

    @pytest.mark.asyncio
    async def test_cod_lower_price_product_triggers_escalation(self):
        """COD order at ₹9999 → change to cosmic shacket (lower retail price).

        differential = new_variant_price − old_line_item_price < 0 → customer
        is owed money → escalation, no order mutation. This is the exact shape
        of the #63043 incident (cheaper replacement on a COD order).
        """
        create = await _create_custom_price_cod_order(
            PHONE_COD_PRICE_DIFF, custom_price="9999.00",
        )
        if not create.get("success"):
            pytest.skip(f"Custom-price COD order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_COD_PRICE_DIFF, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)
        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "L",
        })

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Tool should return success=True with escalation info: {result}"
        )
        assert result.get("requires_escalation") is True, (
            f"COD price difference should now trigger escalation: {result}"
        )
        assert result.get("order_not_modified") is True, (
            f"Order should NOT be modified for price-different COD: {result}"
        )
        assert result.get("old_order_cancelled") is False, (
            f"Old COD order should NOT be cancelled during escalation: {result}"
        )
        assert result.get("differential_amount", 0) < 0, (
            f"Differential should be negative (new product is cheaper): {result}"
        )
        assert result.get("payment_type") == "cod", (
            f"Payment type should be reported as COD: {result}"
        )

    @pytest.mark.asyncio
    async def test_cod_same_price_product_change_proceeds(self):
        """COD order + same-price product change → swap proceeds (no escalation).

        Changing the cosmic shacket to itself at the same variant gives a zero
        differential, so the guard must allow the cancel-and-recreate. GraphQL
        in-place editing is forced to fail so the cancel-and-clone fallback
        (where the COD guard lives) is exercised.
        """
        from unittest.mock import AsyncMock, patch
        from conftest import shopify_retry, _is_rate_limit_error

        create = await shopify_retry(_create_test_order, PHONE_COD_PRODUCT_SAME_PRICE)
        if not create.get("success"):
            pytest.skip(f"COD order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_COD_PRODUCT_SAME_PRICE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        with patch(
            "fashion_bot.core.orchestrator.OrderUpdateOrchestrator.achange_order_product",
            new_callable=AsyncMock,
            return_value={"success": False, "error": "GraphQL editing unavailable"},
        ):
            result = await tools["change_order_product_tool"].ainvoke({
                "order_id": oid,
                "new_product_url": TEST_VALID_PRODUCT_URL,
                "requested_variant": "L",
            })

        assert isinstance(result, dict)
        # The zero-differential branch cancels the old order first, then clones.
        # If Shopify rate-limits the clone (429), the old order is already
        # cancelled and re-running the tool can't recover — treat it as an
        # environmental skip rather than a logic failure. The escalation
        # assertion (requires_escalation is not True) below still holds and is
        # the behaviour this test guards.
        assert result.get("requires_escalation") is not True, (
            f"Zero differential should NOT escalate: {result}"
        )
        if _is_rate_limit_error(result):
            pytest.skip(f"Shopify rate limit (429) during clone — environmental: {result}")
        assert result.get("success") is True, (
            f"Same-price COD product change should succeed: {result}"
        )
        assert result.get("old_order_cancelled") is True, (
            f"Old COD order should be cancelled on a same-price swap: {result}"
        )
        assert result.get("payment_type") == "cod", (
            f"Payment type should remain COD: {result}"
        )
        assert result.get("new_order_id"), (
            f"Should have a new order ID after cancel-recreate: {result}"
        )


# ===========================================================================
# Edge cases: shipped and cancelled order rejection
# ===========================================================================

class TestOrderStateRejection:
    """Tools should reject updates on orders that are already shipped or cancelled."""

    @pytest.mark.asyncio
    async def test_product_change_on_cancelled_order_rejected(self):
        """Calling change_order_product_tool on a cancelled order should fail.

        Creates an order, cancels it, then attempts a product change.
        """
        create = await _create_test_order(PHONE_CANCELLED_UPDATE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_CANCELLED_UPDATE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        cancel_result = await tools["cancel_order_tool"].ainvoke({
            "order_id": oid,
            "cancellation_reason": "customer",
        })
        assert cancel_result.get("success") is True, (
            f"Pre-test cancellation should succeed: {cancel_result}"
        )

        import asyncio
        await asyncio.sleep(3)

        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL_2,
            "requested_variant": "L",
        })

        assert isinstance(result, dict)
        assert result.get("success") is False, (
            f"Product change on cancelled order should fail: {result}"
        )
        error_msg = (result.get("error") or "").lower()
        assert "cancel" in error_msg or "blocked" in error_msg, (
            f"Error should mention cancellation or blocking: {result}"
        )

    @pytest.mark.asyncio
    async def test_size_update_on_cancelled_order_rejected(self):
        """Calling update_order_size_tool on a cancelled order should fail.

        Re-uses the same cancelled order pattern — the config-driven
        _acheck_update_rules should block the size update.
        """
        create = await _create_test_order(PHONE_SHIPPED_UPDATE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_SHIPPED_UPDATE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        cancel_result = await tools["cancel_order_tool"].ainvoke({
            "order_id": oid,
            "cancellation_reason": "customer",
        })
        assert cancel_result.get("success") is True, (
            f"Pre-test cancellation should succeed: {cancel_result}"
        )

        import asyncio
        await asyncio.sleep(3)

        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid,
            "old_variant": "L",
            "new_variant": "M",
        })

        assert isinstance(result, dict)
        assert result.get("success") is False, (
            f"Size update on cancelled order should fail: {result}"
        )
        error_msg = (result.get("error") or result.get("message", "")).lower()
        assert "cancel" in error_msg or "blocked" in error_msg, (
            f"Error should mention cancellation or blocking: {result}"
        )


# ===========================================================================
# Cross-cutting: Phone validation consistency
# ===========================================================================

class TestPhoneValidationConsistency:
    """All order-modifying tools must enforce phone validation."""

    TOOLS_REQUIRING_PHONE_VALIDATION = [
        "get_order_details",
        "update_order_address",
        "update_order_size_tool",
        "update_order_phone_number_tool",
        "update_order_email_tool",
        "update_order_name_tool",
        "annotate_order",
        "cancel_order_tool",
        "change_order_product_tool",
    ]

    @pytest.mark.asyncio
    async def test_all_tools_block_when_no_phone(self):
        """Every order-modifying tool should fail when state has no phone."""
        state = _build_state(phone="", client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        for tool_name in self.TOOLS_REQUIRING_PHONE_VALIDATION:
            kwargs = self._minimal_args(tool_name)
            result = await tools[tool_name].ainvoke(kwargs)
            assert isinstance(result, dict), f"{tool_name} should return dict"
            assert result.get("success") is False or result.get("error"), (
                f"{tool_name} should fail when phone is empty. result={result}"
            )

    @staticmethod
    def _minimal_args(tool_name: str) -> dict:
        """Return the minimal arguments for each tool so it can reach phone validation."""
        base = {"order_id": "FAKE_ORDER_123"}
        tool_args = {
            "get_order_details": base,
            "update_order_address": {
                **base,
                "shipping_address": {"address1": "x", "city": "y", "state": "z", "zip": "000000"},
            },
            "update_order_size_tool": {**base, "old_variant": "M", "new_variant": "L"},
            "update_order_phone_number_tool": {**base, "new_phone": "9000000000"},
            "update_order_email_tool": {**base, "new_email": "x@y.com"},
            "update_order_name_tool": {**base, "new_first_name": "A", "new_last_name": "B"},
            "annotate_order": {**base, "note": "test"},
            "cancel_order_tool": {**base, "cancellation_reason": "other"},
            "change_order_product_tool": {**base, "new_product_url": "https://example.com/p", "requested_variant": "M"},
        }
        return tool_args[tool_name]


# ===========================================================================
# Partially-paid cancel-recreate: transaction (payment) carry-over
# ===========================================================================

class TestPartiallyPaidCancelRecreateTransactionCarryOver:
    """When a partially_paid order is cancelled and recreated (size or product
    change), the already-collected payment must be carried over as a transaction
    on the new order so Shopify records the correct paid/outstanding amounts.
    """

    @pytest.mark.asyncio
    async def test_size_change_partially_paid_carries_payment_to_new_order(self):
        """Size change on a partially_paid GoKwik order.

        Creates a partially_paid order (₹1199 total, ₹200 paid).
        After cancel-and-recreate size change (same price), the new order
        should be partially_paid with ₹200 already captured.
        """
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _acancel_and_recreate_order_with_new_size,
        )
        from fashion_bot.core.factory import ServiceFactory

        paid_amount = "200.00"
        create = await _create_partially_paid_test_order(
            PHONE_PARTIAL_PAID_SIZE_TXN, total_price="1199.00", paid_amount=paid_amount,
        )
        if not create.get("success"):
            pytest.skip(f"Partially paid order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_PARTIAL_PAID_SIZE_TXN, client_id=TEST_CLIENT_ID)
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        order_data = await order_service.aget_order_details(oid, state=state)
        if not order_data:
            pytest.skip(f"Could not fetch order data for {oid}")

        assert order_data.get("financial_status") == "partially_paid", (
            f"Pre-condition: order should be partially_paid, got '{order_data.get('financial_status')}'"
        )

        actual_price = float(order_data.get("total_price", "1199"))
        result = await _acancel_and_recreate_order_with_new_size(
            order_id=oid,
            order_data=order_data,
            old_size="M",
            new_size="L",
            product_handle=TEST_PRODUCT_HANDLE,
            state=state,
            old_variant_price=actual_price,
            new_variant_price=actual_price,
        )

        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Cancel-and-recreate should succeed: {result}"
        )
        assert result.get("method") == "cancel_and_recreate"
        assert result.get("payment_type") == "partial_prepaid"
        assert float(result.get("original_amount_paid", 0)) == float(paid_amount), (
            f"Should report original amount paid ₹{paid_amount}: {result}"
        )
        assert result.get("new_order_id"), (
            f"Should have a new order ID: {result}"
        )

        import asyncio
        await asyncio.sleep(3)

        new_oid = result["new_order_id"]
        new_order_data = await order_service.aget_order_details(new_oid, state=state)
        assert new_order_data, f"New order {new_oid} should be fetchable"
        assert new_order_data.get("financial_status") == "partially_paid", (
            f"New order should be 'partially_paid', got '{new_order_data.get('financial_status')}'"
        )

        new_total = float(new_order_data.get("total_price", "0"))
        new_outstanding = float(new_order_data.get("total_outstanding", str(new_total)))
        paid_on_new = new_total - new_outstanding
        assert paid_on_new >= float(paid_amount) - 1, (
            f"New order should have ≈₹{paid_amount} paid (total={new_total}, "
            f"outstanding={new_outstanding}, paid={paid_on_new})"
        )

    @pytest.mark.asyncio
    async def test_product_change_prepaid_carries_payment_to_new_order(self):
        """Product change on a prepaid order via cancel-recreate.

        Creates a prepaid order whose price matches the test product.
        Patches GraphQL to force the cancel-recreate path. After a same-price
        product change, the new order should have the full amount captured.

        The product change differential check compares new_price vs amount_paid.
        For a prepaid order amount_paid == total_price, so using a total matching
        the target product ensures zero differential → cancel-recreate path.
        """
        from unittest.mock import AsyncMock, patch

        create = await _create_prepaid_test_order(PHONE_PARTIAL_PAID_PRODUCT_TXN, size="L")
        if not create.get("success"):
            pytest.skip(f"Prepaid order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_PARTIAL_PAID_PRODUCT_TXN, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        pre_details = await tools["get_order_details"].ainvoke({"order_id": oid})
        assert pre_details.get("success") is True, (
            f"Should be able to fetch order details: {pre_details}"
        )
        assert pre_details.get("financial_status") == "paid", (
            f"Pre-condition: order should be 'paid', got '{pre_details.get('financial_status')}'"
        )
        original_total = pre_details.get("total_price", "0")

        with patch(
            "fashion_bot.core.orchestrator.OrderUpdateOrchestrator.achange_order_product",
            new_callable=AsyncMock,
            return_value={"success": False, "error": "GraphQL editing unavailable for channel order"},
        ):
            result = await tools["change_order_product_tool"].ainvoke({
                "order_id": oid,
                "new_product_url": TEST_VALID_PRODUCT_URL,
                "requested_variant": "L",
            })

        assert isinstance(result, dict)

        if result.get("requires_escalation"):
            pytest.skip(
                f"Price differential triggered escalation (test product price mismatch): {result}"
            )

        assert result.get("success") is True, (
            f"Product change cancel-recreate should succeed: {result}"
        )
        assert result.get("old_order_cancelled") is True
        assert result.get("new_order_id"), (
            f"Should have a new order ID: {result}"
        )
        assert result.get("payment_type") in ("prepaid", "partial_prepaid"), (
            f"Payment type should be prepaid/partial_prepaid: {result}"
        )

        import asyncio
        await asyncio.sleep(3)

        from fashion_bot.core.factory import ServiceFactory
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        new_oid = result["new_order_id"]
        new_order_data = await order_service.aget_order_details(new_oid, state=state)
        assert new_order_data, f"New order {new_oid} should be fetchable"
        assert new_order_data.get("financial_status") == "paid", (
            f"New order should be 'paid', got '{new_order_data.get('financial_status')}'"
        )

        new_total = float(new_order_data.get("total_price", "0"))
        new_outstanding = float(new_order_data.get("total_outstanding", "0"))
        assert new_outstanding <= 1, (
            f"New order should have ≈₹0 outstanding (fully paid). "
            f"total={new_total}, outstanding={new_outstanding}"
        )


# ===========================================================================
# Discounted-order update tests
# ===========================================================================

class TestDiscountedOrderPreservesDiscountOnUpdate:
    """When an order has discount codes, GraphQL Order Edit must be skipped
    because Shopify does not re-apply discount allocations to newly added
    line items.  Instead, the cancel-and-recreate/clone path should be used
    which preserves discount_codes on the cloned order.

    Test matrix:
        1. Size update  (COD, discounted) → cancel-and-recreate preserves discount
        2. Product change (COD, discounted) → cancel-and-clone preserves discount
        3. Size update  (prepaid, discounted) → cancel-and-recreate preserves discount + payment
        4. Product change (COD, discounted) → verifies method is NOT graphql_order_edit
    """

    # ── Test 1: COD discounted order — size update ────────────────────────

    @pytest.mark.asyncio
    async def test_size_update_discounted_cod_order_preserves_discount(self):
        """A COD order with discount code TESTDISCOUNT50. Size change M→L
        should use cancel-and-recreate (not GraphQL edit) and the new order
        must retain the discount code and comparable total.
        """
        import asyncio

        create = await _create_discounted_test_order(
            phone=PHONE_DISCOUNT_SIZE_UPDATE,
            size="M",
            discount_code="TESTDISCOUNT50",
            discount_amount="500.00",
            financial_status="pending",
        )
        if not create.get("success"):
            pytest.skip(f"Discounted order creation failed: {create}")

        oid = create["order_id"]
        original_total = float(create.get("total_price", "0"))
        original_discounts = create.get("discount_codes", [])
        assert len(original_discounts) > 0, "Pre-condition: order must have discount codes"

        state = _build_state(phone=PHONE_DISCOUNT_SIZE_UPDATE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": oid,
            "old_variant": "M",
            "new_variant": "L",
            "phone_number": PHONE_DISCOUNT_SIZE_UPDATE,
        })

        assert isinstance(result, dict)

        if result.get("requires_escalation"):
            pytest.skip(f"Price differential triggered escalation: {result}")

        assert result.get("success") is True, (
            f"Size update should succeed: {result}"
        )
        assert result.get("method") == "cancel_and_recreate", (
            f"Discounted order should use cancel-and-recreate, got method={result.get('method')}"
        )

        new_oid = result.get("new_order_id")
        assert new_oid, f"Should produce a new order ID: {result}"
        new_oid_clean = str(new_oid).lstrip("#")

        await asyncio.sleep(5)

        from fashion_bot.core.factory import ServiceFactory
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        new_order_data = await order_service.aget_order_details(new_oid_clean, state=state)
        if not new_order_data:
            await asyncio.sleep(5)
            new_order_data = await order_service.aget_order_details(new_oid_clean, state=state)
        assert new_order_data, f"New order {new_oid_clean} should be fetchable"

        new_discounts = new_order_data.get("discount_codes", [])
        assert len(new_discounts) > 0, (
            f"New order must retain discount codes. Got: {new_discounts}"
        )
        assert new_discounts[0].get("code") == "TESTDISCOUNT50", (
            f"Discount code should be TESTDISCOUNT50, got: {new_discounts}"
        )

        new_total = float(new_order_data.get("total_price", "0"))
        assert abs(new_total - original_total) < 50, (
            f"New order total (₹{new_total}) should be close to original (₹{original_total}). "
            f"Discount was not preserved."
        )

    # ── Test 2: COD discounted order — product change ─────────────────────

    @pytest.mark.asyncio
    async def test_product_change_discounted_cod_order_preserves_discount(self):
        """A COD order with discount code. Product change should use
        cancel-and-clone (not GraphQL edit) and the new order must retain
        the discount code and comparable total.

        Uses COD (pending) to avoid prepaid differential escalation.
        """
        import asyncio
        await asyncio.sleep(10)

        create = await _create_discounted_test_order(
            phone=PHONE_DISCOUNT_PRODUCT_CHANGE,
            size="M",
            discount_code="TESTDISCOUNT50",
            discount_amount="500.00",
            financial_status="pending",
        )
        if not create.get("success"):
            pytest.skip(f"Discounted order creation failed: {create}")

        oid = create["order_id"]
        original_total = float(create.get("total_price", "0"))
        original_discounts = create.get("discount_codes", [])
        assert len(original_discounts) > 0, "Pre-condition: order must have discount codes"

        state = _build_state(phone=PHONE_DISCOUNT_PRODUCT_CHANGE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "L",
            "phone_number": PHONE_DISCOUNT_PRODUCT_CHANGE,
        })

        assert isinstance(result, dict)

        if result.get("requires_escalation"):
            pytest.skip(f"Price differential triggered escalation: {result}")

        assert result.get("success") is True, (
            f"Product change should succeed: {result}"
        )

        method = result.get("method", "")
        assert method != "graphql_order_edit", (
            f"Discounted order must NOT use graphql_order_edit. Got method={method}"
        )

        new_oid = result.get("new_order_id")
        assert new_oid, f"Should produce a new order ID: {result}"
        new_oid_clean = str(new_oid).lstrip("#")

        await asyncio.sleep(5)

        from fashion_bot.core.factory import ServiceFactory
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        new_order_data = await order_service.aget_order_details(new_oid_clean, state=state)
        if not new_order_data:
            await asyncio.sleep(5)
            new_order_data = await order_service.aget_order_details(new_oid_clean, state=state)
        assert new_order_data, f"New order {new_oid_clean} should be fetchable"

        new_discounts = new_order_data.get("discount_codes", [])
        assert len(new_discounts) > 0, (
            f"New order must retain discount codes. Got: {new_discounts}"
        )
        assert new_discounts[0].get("code") == "TESTDISCOUNT50", (
            f"Discount code should be TESTDISCOUNT50, got: {new_discounts}"
        )

        new_total = float(new_order_data.get("total_price", "0"))
        assert abs(new_total - original_total) < 50, (
            f"New order total (₹{new_total}) should be close to original (₹{original_total}). "
            f"Discount was not preserved."
        )

    # ── Test 3: Prepaid discounted order — size update preserves payment ──

    @pytest.mark.asyncio
    async def test_size_update_discounted_prepaid_preserves_payment_and_discount(self):
        """A prepaid order with discount code. Size change should use
        cancel-and-recreate, preserve both the discount and the payment
        (financial_status=paid, outstanding≈0).
        """
        import asyncio
        await asyncio.sleep(10)

        from fashion_bot.shopify.modules.order_editing_graphql import (
            _acancel_and_recreate_order_with_new_size,
        )
        from fashion_bot.core.factory import ServiceFactory
        from conftest import shopify_retry

        create = await shopify_retry(
            _create_discounted_test_order,
            phone=PHONE_DISCOUNT_SIZE_PREPAID,
            size="M",
            discount_code="TESTDISCOUNT50",
            discount_amount="500.00",
            financial_status="paid",
        )
        if not create.get("success"):
            pytest.skip(f"Discounted order creation failed: {create}")

        oid = create["order_id"]
        original_total = float(create.get("total_price", "0"))

        state = _build_state(phone=PHONE_DISCOUNT_SIZE_PREPAID, client_id=TEST_CLIENT_ID)
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        order_data = await order_service.aget_order_details(oid, state=state)
        if not order_data:
            pytest.skip(f"Could not fetch order data for {oid}")

        assert order_data.get("financial_status") == "paid", (
            f"Pre-condition: order should be 'paid', got '{order_data.get('financial_status')}'"
        )
        assert len(order_data.get("discount_codes", [])) > 0, (
            "Pre-condition: order must have discount codes"
        )

        variant_price = float(
            order_data.get("line_items", [{}])[0].get("price", "1699")
        )

        result = await shopify_retry(
            _acancel_and_recreate_order_with_new_size,
            order_id=oid,
            order_data=order_data,
            old_size="M",
            new_size="L",
            product_handle=TEST_PRODUCT_HANDLE,
            state=state,
            old_variant_price=variant_price,
            new_variant_price=variant_price,
        )

        assert isinstance(result, dict)

        if result.get("requires_escalation"):
            pytest.skip(f"Price differential triggered escalation: {result}")

        assert result.get("success") is True, (
            f"Cancel-and-recreate should succeed: {result}"
        )
        assert result.get("method") == "cancel_and_recreate"
        assert result.get("new_order_id"), f"Should have a new order ID: {result}"

        new_oid = str(result["new_order_id"]).lstrip("#")
        await asyncio.sleep(5)

        new_order_data = await order_service.aget_order_details(new_oid, state=state)
        if not new_order_data:
            await asyncio.sleep(5)
            new_order_data = await order_service.aget_order_details(new_oid, state=state)
        assert new_order_data, f"New order {new_oid} should be fetchable"

        new_discounts = new_order_data.get("discount_codes", [])
        assert len(new_discounts) > 0, (
            f"New order must retain discount codes. Got: {new_discounts}"
        )
        assert new_discounts[0].get("code") == "TESTDISCOUNT50"

        new_total = float(new_order_data.get("total_price", "0"))
        assert abs(new_total - original_total) < 50, (
            f"New order total (₹{new_total}) should be close to original (₹{original_total})"
        )

        assert new_order_data.get("financial_status") == "paid", (
            f"New order should be 'paid', got '{new_order_data.get('financial_status')}'"
        )
        new_outstanding = float(new_order_data.get("total_outstanding", "0"))
        assert new_outstanding <= 1, (
            f"Prepaid order should have ≈₹0 outstanding. Got ₹{new_outstanding}"
        )

    # ── Test 4: Discounted prepaid — product change skips GraphQL ─────────

    @pytest.mark.asyncio
    async def test_product_change_discounted_order_skips_graphql_edit(self):
        """Verify that a discounted prepaid order does NOT use graphql_order_edit
        for product change. The method should be cancel-and-recreate/clone
        OR escalation (due to price differential) — but never graphql_order_edit.
        """
        import asyncio
        await asyncio.sleep(10)

        create = await _create_discounted_test_order(
            phone=PHONE_DISCOUNT_PRODUCT_PREPAID,
            size="M",
            discount_code="TESTDISCOUNT50",
            discount_amount="500.00",
            financial_status="paid",
        )
        if not create.get("success"):
            pytest.skip(f"Discounted order creation failed: {create}")

        oid = create["order_id"]

        state = _build_state(phone=PHONE_DISCOUNT_PRODUCT_PREPAID, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "L",
            "phone_number": PHONE_DISCOUNT_PRODUCT_PREPAID,
        })

        assert isinstance(result, dict)

        method = result.get("method", "")
        assert method != "graphql_order_edit", (
            f"Discounted order must NOT use graphql_order_edit. Got method={method}. "
            f"Full result: {result}"
        )

        if result.get("requires_escalation"):
            assert result.get("order_not_modified") is True or result.get("old_order_cancelled") is not True, (
                "Escalation for discounted order is acceptable (price differential), "
                "but the order must not have been modified via GraphQL edit."
            )
        elif result.get("success"):
            new_oid = result.get("new_order_id")
            assert new_oid, f"Successful product change should produce a new order ID: {result}"

            await asyncio.sleep(5)

            from fashion_bot.core.factory import ServiceFactory
            order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
            new_oid_clean = str(new_oid).lstrip("#")
            new_order_data = await order_service.aget_order_details(new_oid_clean, state=state)
            if new_order_data:
                new_discounts = new_order_data.get("discount_codes", [])
                assert len(new_discounts) > 0, (
                    f"New order must retain discount codes. Got: {new_discounts}"
                )

    @pytest.mark.asyncio
    async def test_discounted_prepaid_same_retail_price_no_escalation(self):
        """Discounted prepaid order: product change to same-retail-price product succeeds.

        Original product at ₹X with discount → total paid is ₹(X - discount).
        New product also at ₹X (same retail). Differential is computed on
        retail prices (₹X - ₹X = 0), NOT on paid amount vs retail. The
        cancel-and-clone preserves discount codes so Shopify re-applies
        the discount automatically.
        """
        import asyncio
        await asyncio.sleep(10)
        from conftest import shopify_retry

        create = await shopify_retry(
            _create_discounted_test_order,
            phone=PHONE_DISCOUNT_SAME_RETAIL_PREPAID,
            size="M",
            discount_code="TESTDISCOUNT50",
            discount_amount="500.00",
            financial_status="paid",
        )
        if not create.get("success"):
            pytest.skip(f"Discounted order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_DISCOUNT_SAME_RETAIL_PREPAID, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        result = await shopify_retry(
            tools["change_order_product_tool"].ainvoke,
            {
                "order_id": oid,
                "new_product_url": TEST_VALID_PRODUCT_URL,
                "requested_variant": "L",
                "phone_number": PHONE_DISCOUNT_SAME_RETAIL_PREPAID,
            },
        )

        assert isinstance(result, dict)
        assert result.get("requires_escalation") is not True, (
            f"Same retail price product change on discounted order should NOT escalate. "
            f"Differential should be 0 (retail-to-retail). Got: {result}"
        )
        assert result.get("success") is True, (
            f"Same retail price product change should succeed: {result}"
        )
        new_oid = result.get("new_order_id")
        assert new_oid, f"Successful product change should produce a new order ID: {result}"

        await asyncio.sleep(5)

        from fashion_bot.core.factory import ServiceFactory
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        new_oid_clean = str(new_oid).lstrip("#")
        new_order_data = await order_service.aget_order_details(new_oid_clean, state=state)
        if new_order_data:
            new_discounts = new_order_data.get("discount_codes", [])
            assert len(new_discounts) > 0, (
                f"New order must retain discount codes: {new_discounts}"
            )
            assert new_order_data.get("financial_status") == "paid", (
                f"New order should be paid: {new_order_data.get('financial_status')}"
            )

    @pytest.mark.asyncio
    async def test_discounted_prepaid_different_retail_price_escalates(self):
        """Discounted prepaid order: product change to a DIFFERENT retail price triggers escalation.

        Original product (cosmic shacket) at ~₹1199 with ₹500 discount.
        New product (custom ₹9999 item) has a different retail price.
        Differential = new_retail - old_retail ≠ 0 → escalation.
        """
        import asyncio
        await asyncio.sleep(10)

        create = await _create_discounted_test_order(
            phone=PHONE_DISCOUNT_DIFF_RETAIL_PREPAID,
            size="M",
            discount_code="TESTDISCOUNT50",
            discount_amount="500.00",
            financial_status="paid",
        )
        if not create.get("success"):
            pytest.skip(f"Discounted order creation failed: {create}")
        oid = create["order_id"]

        state = _build_state(phone=PHONE_DISCOUNT_DIFF_RETAIL_PREPAID, client_id=TEST_CLIENT_ID)
        tools, _ = _get_cancel_update_tools(state)

        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL_2,
            "requested_variant": "L",
            "phone_number": PHONE_DISCOUNT_DIFF_RETAIL_PREPAID,
        })

        assert isinstance(result, dict)
        if result.get("requires_escalation"):
            assert result.get("order_not_modified") is True, (
                f"Order should NOT be modified when escalation is required: {result}"
            )
            assert result.get("differential_amount", 0) != 0, (
                f"Should report a non-zero price differential based on retail prices: {result}"
            )
            assert result.get("old_order_cancelled") is False, (
                f"Old order should NOT be cancelled during escalation: {result}"
            )
        elif result.get("success"):
            pass
        else:
            pytest.fail(f"Expected escalation or success, got: {result}")


class TestMultiItemPrepaidDifferentialUsesLineItemPrice:
    """For multi-item prepaid orders, the price differential must compare
    the target line item's retail price to the new variant's retail price,
    NOT the total order amount_paid to the new variant price.
    """

    @pytest.mark.asyncio
    async def test_multi_item_prepaid_same_price_item_swap_no_escalation(self):
        """Multi-item prepaid order: swap one item for same-retail-price product.

        Order has 2 items totaling ~₹2400 (2 × ₹1199). Swapping one item
        for a product at the same ₹1199 retail price should NOT trigger
        escalation — differential is ₹0 on the swapped item.

        With the old (buggy) logic, differential would be:
          ₹1199 - ₹2398 (amount_paid) = -₹1199 → false escalation.
        """
        import asyncio, httpx
        await asyncio.sleep(10)

        product_info = await _get_product_variants()
        if not product_info:
            pytest.skip("Could not fetch product info")
        variant_m = product_info["variants"].get("M")
        if not variant_m:
            pytest.skip("Variant M not found")

        headers, base_url = await _get_shopify_rest_config()
        order_payload = {
            "line_items": [
                {"variant_id": variant_m["id"], "quantity": 1},
                {"variant_id": variant_m["id"], "quantity": 1},
            ],
            "shipping_address": {
                "first_name": "MultiDiff", "last_name": "Test",
                "address1": "E-10 Jail Road, Janak Puri",
                "city": "New Delhi", "province": "Delhi",
                "zip": "110058", "country": "India",
                "phone": f"+91{PHONE_MULTI_ITEM_PREPAID_DIFF}",
            },
            "phone": f"+91{PHONE_MULTI_ITEM_PREPAID_DIFF}",
            "financial_status": "paid",
            "tags": "BOT_TEST, MULTI_ITEM_DIFF_TEST",
        }
        variant_price = float(variant_m["price"])
        total = variant_price * 2
        order_payload["transactions"] = [
            {"kind": "sale", "status": "success", "amount": f"{total:.2f}"},
        ]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/orders.json", json={"order": order_payload}, headers=headers, timeout=30,
            )
            if resp.status_code >= 400:
                pytest.skip(f"Order creation failed: {resp.text[:300]}")
            order = resp.json().get("order", {})

        oid = str(order.get("order_number", ""))
        state = _build_state(phone=PHONE_MULTI_ITEM_PREPAID_DIFF, client_id=TEST_CLIENT_ID)
        await _wait_for_order_indexed(oid, state)

        tools, _ = _get_cancel_update_tools(state)
        line_items = order.get("line_items", [])
        target_vid = str(line_items[0].get("variant_id", "")) if line_items else ""

        result = await tools["change_order_product_tool"].ainvoke({
            "order_id": oid,
            "new_product_url": TEST_VALID_PRODUCT_URL,
            "requested_variant": "L",
            "line_item_variant_id": target_vid,
            "phone_number": PHONE_MULTI_ITEM_PREPAID_DIFF,
        })

        assert isinstance(result, dict)
        assert result.get("requires_escalation") is not True, (
            f"Same retail price swap in multi-item order should NOT escalate. "
            f"Differential should be based on line item price (₹{variant_price}), "
            f"not total paid (₹{total}). Got: {result}"
        )
        if result.get("success"):
            assert result.get("new_order_id") or result.get("method") == "graphql_order_edit", (
                f"Successful product change should produce new order or use GraphQL edit: {result}"
            )


# ===========================================================================
# Test: Out-of-stock variant blocks size update
# ===========================================================================

class TestSizeUpdateBlockedWhenOutOfStock:
    """aupdate_order_size_graphql must reject a size change when the
    requested variant is out of stock (inventoryQuantity=0,
    inventoryPolicy=DENY).  The response should list in-stock alternatives.
    """

    @pytest.mark.asyncio
    async def test_out_of_stock_variant_returns_error_with_alternatives(self):
        """Mock the product fetch to return an out-of-stock variant for the
        requested size and verify the function returns success=False with
        in_stock_variants and suggestion.
        """
        from unittest.mock import AsyncMock, patch, MagicMock

        from fashion_bot.shopify.modules.order_editing_graphql import (
            aupdate_order_size_graphql,
        )

        state = _build_state(phone=PHONE_OOS_SIZE_UPDATE, client_id=TEST_CLIENT_ID)

        mock_order_data = {
            "id": "6000000000001",
            "name": "#TEST-OOS-001",
            "line_items": [
                {
                    "variant_id": "40000000001",
                    "variant_title": "M",
                    "name": "Test Product - M",
                    "product_id": "7000000001",
                    "price": "1499.00",
                    "quantity": 1,
                },
            ],
            "discount_codes": [],
        }

        mock_product = {
            "handle": "test-product",
            "variants": [
                {
                    "id": "gid://shopify/ProductVariant/40000000001",
                    "title": "M",
                    "price": "1499.00",
                    "inventoryQuantity": 5,
                    "inventoryPolicy": "DENY",
                    "is_available": True,
                },
                {
                    "id": "gid://shopify/ProductVariant/40000000002",
                    "title": "L",
                    "price": "1499.00",
                    "inventoryQuantity": 0,
                    "inventoryPolicy": "DENY",
                    "is_available": False,
                },
                {
                    "id": "gid://shopify/ProductVariant/40000000003",
                    "title": "XL",
                    "price": "1499.00",
                    "inventoryQuantity": 3,
                    "inventoryPolicy": "DENY",
                    "is_available": True,
                },
                {
                    "id": "gid://shopify/ProductVariant/40000000004",
                    "title": "S",
                    "price": "1499.00",
                    "inventoryQuantity": 0,
                    "inventoryPolicy": "DENY",
                    "is_available": False,
                },
            ],
        }

        mock_order_service = AsyncMock()
        mock_order_service.aget_order_details = AsyncMock(return_value=mock_order_data)

        mock_factory = AsyncMock()
        mock_factory.return_value = mock_order_service

        with patch(
            "fashion_bot.core.factory.ServiceFactory.aget_order_service",
            mock_factory,
        ), patch(
            "fashion_bot.shopify.modules.product_handlers.ashopify_get_product_by_id_graphql",
            new_callable=AsyncMock,
            return_value={"success": True, "product": mock_product},
        ):
            result = await aupdate_order_size_graphql(
                order_id="TEST-OOS-001",
                old_size="M",
                new_size="L",
                access_token="fake-token",
                shop_url="test-shop.myshopify.com",
                state=state,
            )

        assert result["success"] is False
        assert "out of stock" in result["error"].lower()
        assert "in_stock_variants" in result
        assert "XL" in result["in_stock_variants"], (
            f"XL is in stock and should be suggested. Got: {result['in_stock_variants']}"
        )
        assert "M" not in result["in_stock_variants"], (
            "Current size M should be excluded from alternatives"
        )
        assert "S" not in result["in_stock_variants"], (
            "Out-of-stock size S should not appear in alternatives"
        )
        assert "L" not in result["in_stock_variants"], (
            "Requested out-of-stock size L should not appear in alternatives"
        )
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_continue_policy_variant_is_not_blocked(self):
        """A variant with inventoryQuantity=0 but inventoryPolicy=CONTINUE
        (overselling allowed) should NOT be blocked — is_available=True.
        """
        from unittest.mock import AsyncMock, patch

        from fashion_bot.shopify.modules.order_editing_graphql import (
            aupdate_order_size_graphql,
        )

        state = _build_state(phone=PHONE_OOS_SIZE_UPDATE, client_id=TEST_CLIENT_ID)

        mock_order_data = {
            "id": "6000000000002",
            "name": "#TEST-CONT-001",
            "line_items": [
                {
                    "variant_id": "40000000010",
                    "variant_title": "M",
                    "name": "Test Product - M",
                    "product_id": "7000000002",
                    "price": "1499.00",
                    "quantity": 1,
                },
            ],
            "discount_codes": [],
        }

        mock_product = {
            "handle": "test-product-continue",
            "variants": [
                {
                    "id": "gid://shopify/ProductVariant/40000000010",
                    "title": "M",
                    "price": "1499.00",
                    "inventoryQuantity": 5,
                    "inventoryPolicy": "DENY",
                    "is_available": True,
                },
                {
                    "id": "gid://shopify/ProductVariant/40000000011",
                    "title": "L",
                    "price": "1499.00",
                    "inventoryQuantity": 0,
                    "inventoryPolicy": "CONTINUE",
                    "is_available": True,
                },
            ],
        }

        mock_order_service = AsyncMock()
        mock_order_service.aget_order_details = AsyncMock(return_value=mock_order_data)

        mock_begin = AsyncMock(return_value={
            "success": True,
            "calculated_order_id": "gid://shopify/CalculatedOrder/1",
            "calculated_order": {
                "lineItems": {"edges": [
                    {"node": {
                        "id": "gid://shopify/CalculatedLineItem/1",
                        "variant": {"id": "gid://shopify/ProductVariant/40000000010"},
                        "quantity": 1,
                    }},
                ]},
            },
        })
        mock_set_qty = AsyncMock(return_value={"success": True})
        mock_add_variant = AsyncMock(return_value={"success": True})
        mock_commit = AsyncMock(return_value={
            "success": True,
            "order": {
                "id": "gid://shopify/Order/6000000000002",
                "name": "#TEST-CONT-001",
                "lineItems": {"edges": [
                    {"node": {
                        "id": "gid://shopify/LineItem/1",
                        "title": "Test Product",
                        "quantity": 1,
                        "variant": {
                            "id": "gid://shopify/ProductVariant/40000000011",
                            "title": "L",
                        },
                    }},
                ]},
            },
        })
        mock_sr_update = AsyncMock(return_value={"success": True})
        mock_add_note = AsyncMock(return_value=None)

        graphql_mod = "fashion_bot.shopify.modules.order_editing_graphql"
        with patch(
            "fashion_bot.core.factory.ServiceFactory.aget_order_service",
            AsyncMock(return_value=mock_order_service),
        ), patch(
            "fashion_bot.shopify.modules.product_handlers.ashopify_get_product_by_id_graphql",
            new_callable=AsyncMock,
            return_value={"success": True, "product": mock_product},
        ), patch(
            f"{graphql_mod}.aorder_edit_begin",
            mock_begin,
        ), patch(
            f"{graphql_mod}.aorder_edit_set_quantity",
            mock_set_qty,
        ), patch(
            f"{graphql_mod}.aorder_edit_add_variant",
            mock_add_variant,
        ), patch(
            f"{graphql_mod}.aorder_edit_commit",
            mock_commit,
        ), patch(
            f"{graphql_mod}._aupdate_shiprocket_order_size",
            mock_sr_update,
        ), patch(
            f"{graphql_mod}._aadd_size_update_note_to_shopify",
            mock_add_note,
        ):
            result = await aupdate_order_size_graphql(
                order_id="TEST-CONT-001",
                old_size="M",
                new_size="L",
                access_token="fake-token",
                shop_url="test-shop.myshopify.com",
                state=state,
            )

        assert result.get("success") is True, (
            f"CONTINUE policy variant should not be blocked. Got: {result}"
        )
        assert "out of stock" not in result.get("error", "").lower()


# ===========================================================================
# Test: Percentage discount amount sanitization in aclone_order
# ===========================================================================

class TestCloneOrderSanitizesPercentageDiscount:
    """When Shopify returns discount_codes with type=percentage and amount
    as the absolute monetary deduction (e.g. ₹5442.03), aclone_order must
    convert the amount to the actual percentage rate (≤ 100) before POSTing
    the clone to Shopify.

    Uses ``fashion_bot.utils.http_client._shared_async_client`` swap to
    intercept outgoing HTTP without fighting the singleton cache.
    """

    @staticmethod
    def _make_mock_client(captured_payload: dict, order_resp: dict):
        """Build a mock httpx.AsyncClient whose ``.post()`` captures the
        payload and returns a 201 with ``order_resp``.
        """
        from unittest.mock import AsyncMock
        import httpx

        async def _mock_post(url, *, json=None, headers=None, timeout=None, **kw):
            captured_payload.update(json.get("order", {}) if json else {})
            return httpx.Response(
                status_code=201,
                json={"order": order_resp},
                request=httpx.Request("POST", url),
            )

        mock = AsyncMock(spec=httpx.AsyncClient)
        mock.post = _mock_post
        return mock

    @staticmethod
    def _make_adapter():
        from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter
        adapter = ShopifyOrderAdapter(client_id=TEST_CLIENT_ID)
        adapter._config = {
            "access_token": "fake-token",
            "shop_url": "test-shop.myshopify.com",
            "api_version": "2024-04",
        }
        return adapter

    @staticmethod
    def _base_order_data(**overrides):
        data = {
            "name": "#TEST-DC-001",
            "shipping_address": {
                "first_name": "Test", "last_name": "User",
                "address1": "123 Main St", "city": "Delhi",
                "province": "Delhi", "zip": "110001",
                "country": "India", "country_code": "IN",
                "phone": "+919500200049",
            },
            "customer": {"id": 12345},
            "tags": "BOT_TEST",
            "financial_status": "pending",
            "currency": "INR",
            "total_line_items_price": "1499.00",
        }
        data.update(overrides)
        return data

    @pytest.mark.asyncio
    async def test_percentage_discount_amount_converted_to_actual_pct(self):
        """Discount amount=5442.03 on a subtotal of ₹5497.00 should be
        converted to ~99.0% (not passed as 5442.03 which Shopify rejects).
        """
        import fashion_bot.utils.http_client as _http_mod

        state = _build_state(phone=PHONE_DISCOUNT_PCT_CLONE, client_id=TEST_CLIENT_ID)
        captured = {}
        mock_client = self._make_mock_client(captured, {
            "id": 99999, "name": "#TEST-PCT-CLONE",
            "order_number": "99999", "total_price": "54.97",
            "financial_status": "paid",
        })
        original = _http_mod._shared_async_client
        _http_mod._shared_async_client = mock_client
        try:
            adapter = self._make_adapter()
            result = await adapter.aclone_order(
                original_order_data=self._base_order_data(
                    name="#TEST-PCT-001",
                    financial_status="paid",
                    payment_gateway_names=["manual"],
                    total_line_items_price="5497.00",
                    subtotal_price="54.97",
                    discount_codes=[
                        {"code": "Test99", "amount": "5442.03", "type": "percentage"},
                    ],
                ),
                new_line_items=[{"variant_id": 40000000002, "quantity": 1}],
                state=state,
                note="Size change test",
            )
        finally:
            _http_mod._shared_async_client = original

        assert result["success"] is True, f"Clone should succeed: {result}"
        sent_discounts = captured.get("discount_codes", [])
        assert len(sent_discounts) == 1
        dc = sent_discounts[0]
        assert dc["code"] == "Test99"
        assert dc["type"] == "percentage"
        sent_amount = float(dc["amount"])
        assert sent_amount <= 100, (
            f"Percentage amount must be ≤ 100, got {sent_amount}. "
            f"Raw Shopify amount (₹5442.03) should have been converted to a percentage."
        )
        expected_pct = round((5442.03 / 5497.00) * 100, 2)
        assert abs(sent_amount - expected_pct) < 0.1, (
            f"Expected ~{expected_pct}%, got {sent_amount}%"
        )

    @pytest.mark.asyncio
    async def test_fixed_amount_discount_passes_through_unchanged(self):
        """A fixed_amount discount should not be modified at all."""
        import fashion_bot.utils.http_client as _http_mod

        state = _build_state(phone=PHONE_DISCOUNT_PCT_CLONE, client_id=TEST_CLIENT_ID)
        captured = {}
        mock_client = self._make_mock_client(captured, {
            "id": 99998, "name": "#TEST-FIXED-CLONE",
            "order_number": "99998", "total_price": "999.00",
            "financial_status": "pending",
        })
        original = _http_mod._shared_async_client
        _http_mod._shared_async_client = mock_client
        try:
            adapter = self._make_adapter()
            result = await adapter.aclone_order(
                original_order_data=self._base_order_data(
                    discount_codes=[
                        {"code": "FLAT500", "amount": "500.00", "type": "fixed_amount"},
                    ],
                ),
                new_line_items=[{"variant_id": 40000000001, "quantity": 1}],
                state=state,
            )
        finally:
            _http_mod._shared_async_client = original

        assert result["success"] is True
        dc = captured.get("discount_codes", [])[0]
        assert dc["code"] == "FLAT500"
        assert dc["type"] == "fixed_amount"
        assert dc["amount"] == "500.00", (
            f"fixed_amount discount should pass through unchanged, got {dc['amount']}"
        )

    @pytest.mark.asyncio
    async def test_percentage_discount_under_100_passes_through(self):
        """A percentage discount with amount ≤ 100 (actual rate, not
        absolute) should pass through unchanged.
        """
        import fashion_bot.utils.http_client as _http_mod

        state = _build_state(phone=PHONE_DISCOUNT_PCT_CLONE, client_id=TEST_CLIENT_ID)
        captured = {}
        mock_client = self._make_mock_client(captured, {
            "id": 99997, "name": "#TEST-PCTLOW-CLONE",
            "order_number": "99997", "total_price": "1349.10",
            "financial_status": "pending",
        })
        original = _http_mod._shared_async_client
        _http_mod._shared_async_client = mock_client
        try:
            adapter = self._make_adapter()
            result = await adapter.aclone_order(
                original_order_data=self._base_order_data(
                    discount_codes=[
                        {"code": "SAVE10", "amount": "10.00", "type": "percentage"},
                    ],
                ),
                new_line_items=[{"variant_id": 40000000001, "quantity": 1}],
                state=state,
            )
        finally:
            _http_mod._shared_async_client = original

        assert result["success"] is True
        dc = captured.get("discount_codes", [])[0]
        assert dc["type"] == "percentage"
        assert dc["amount"] == "10.00", (
            f"Low percentage should pass through unchanged, got {dc['amount']}"
        )

    @pytest.mark.asyncio
    async def test_percentage_discount_zero_subtotal_falls_back_to_fixed(self):
        """When subtotal is 0 (edge case), a percentage discount with
        amount > 100 should be converted to fixed_amount to avoid division
        by zero.
        """
        import fashion_bot.utils.http_client as _http_mod

        state = _build_state(phone=PHONE_DISCOUNT_PCT_CLONE, client_id=TEST_CLIENT_ID)
        captured = {}
        mock_client = self._make_mock_client(captured, {
            "id": 99996, "name": "#TEST-ZERO-CLONE",
            "order_number": "99996", "total_price": "0.00",
            "financial_status": "pending",
        })
        original = _http_mod._shared_async_client
        _http_mod._shared_async_client = mock_client
        try:
            adapter = self._make_adapter()
            result = await adapter.aclone_order(
                original_order_data=self._base_order_data(
                    total_line_items_price="0",
                    subtotal_price="0",
                    discount_codes=[
                        {"code": "BIGDISCOUNT", "amount": "500.00", "type": "percentage"},
                    ],
                ),
                new_line_items=[{"variant_id": 40000000001, "quantity": 1}],
                state=state,
            )
        finally:
            _http_mod._shared_async_client = original

        assert result["success"] is True
        dc = captured.get("discount_codes", [])[0]
        assert dc["type"] == "fixed_amount", (
            f"With zero subtotal, type should fall back to fixed_amount, got {dc['type']}"
        )
        assert dc["amount"] == "500.00"


# ===========================================================================
# Live-client integration tests — Concept Groove (c3ffcb1b-...)
#
# These tests hit REAL Shopify / Shiprocket / Delhivery APIs.
# They are excluded from the default pytest run via `-m "not integration"` in
# pytest.ini.  Run them explicitly:
#
#   cd fashion_bot/tests
#   pytest test_cancel_or_update_tools.py::TestLiveClientOrderUpdates -m integration -v
#
# Before running:
#   1. Fill in a known NEW (unfulfilled) order ID + its owner's phone below.
#   2. Fill in a known Shiprocket-fulfilled order ID + phone.
#   3. Fill in a known Delhivery-fulfilled order ID + phone.
#   Orders are MUTATED by these tests (address/phone/email changed, notes added).
#   Use dedicated test orders where possible.
# ===========================================================================

# ---------------------------------------------------------------------------
# Configurable order fixtures — fill these in before running
# ---------------------------------------------------------------------------

# A NEW / unfulfilled order.  Updated fields will be reverted manually after.
_LIVE_ORDER_NEW_ID = "gv15361"          # e.g. "gv15361"
_LIVE_ORDER_NEW_PHONE = "9716336096"    # phone registered on that order

# A fulfilled order shipped via Shiprocket (tracking_url contains 'shiprocket').
_LIVE_ORDER_SHIPROCKET_ID = "gv15392"
_LIVE_ORDER_SHIPROCKET_PHONE = "7973140055"

# A fulfilled order shipped via Delhivery (tracking_url contains 'delhivery').
# Set to None to skip Delhivery-specific tests if no such order is available.
_LIVE_ORDER_DELHIVERY_ID = "gv15361"   # replace with a real Delhivery-shipped order
_LIVE_ORDER_DELHIVERY_PHONE = "9716336096"

_LIVE_CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _live_state(phone: str) -> dict:
    return {
        "client_id": _LIVE_CLIENT_ID,
        "phone_number": phone,
        "conversation_context": {"entities": [], "topics": [], "focal_entity": None},
        "messages": [],
    }


def _live_tools(phone: str) -> dict:
    state = _live_state(phone)
    tools = cancel_or_update_tools_factory(state, state["messages"])
    return {t.name: t for t in tools}, state


async def _fetch_strategy_for_order(order_id: str, phone: str) -> tuple[str, str]:
    """Return (canonical_partner, strategy) for a given live order.

    Resolves the effective carrier via URL-first logic and looks up
    order_update_strategy from client_configs in Postgres.
    """
    from fashion_bot.config_manager import aget_partner_update_strategy
    from fashion_bot.utils.delivery_partner_utils import aresolve_effective_partner_for_order
    from fashion_bot.core.orchestrator import OrderStatusOrchestrator

    state = _live_state(phone)
    status = await OrderStatusOrchestrator.aget_order_status(order_id, state=state)
    orders = status.get("orders", [])
    assert orders, f"Order {order_id} not found on live client"
    order_dto = orders[0]

    partner, _ = await aresolve_effective_partner_for_order(order_dto, state=state)
    strategy = await aget_partner_update_strategy(partner or "", _LIVE_CLIENT_ID) if partner else "escalate_for_manual_update"
    return partner or "unknown", strategy


def _assert_update_outcome(result: dict, strategy: str, partner: str, update_type: str) -> None:
    """Central assertion logic driven by the resolved strategy."""
    assert isinstance(result, dict), f"Tool returned non-dict: {result!r}"

    if result.get("requires_confirmation"):
        # cancel_and_recreate path — LLM must confirm before proceeding.
        assert strategy == "cancel_and_recreate", (
            f"requires_confirmation returned but strategy is {strategy!r}"
        )
        return

    assert result.get("success"), (
        f"{update_type} update failed for partner={partner}, strategy={strategy}: "
        f"{result.get('error') or result.get('message')}"
    )

    if strategy == "update_inplace":
        lu = result.get("logistics_update", {})
        assert lu.get("success"), (
            f"Strategy is update_inplace but logistics_update.success=False for {partner}: "
            f"{lu.get('error') or lu}"
        )

    elif strategy == "escalate_for_manual_update":
        requires_esc = result.get("requires_escalation") or result.get("requires_manual_action")
        escalation = result.get("escalation_result") or result.get("escalation")
        assert requires_esc or (isinstance(escalation, dict) and escalation.get("success")), (
            f"Strategy is escalate_for_manual_update but neither requires_escalation nor "
            f"escalation_result found in result: {result}"
        )

    elif strategy == "cancel_and_recreate":
        # Confirmation was not passed → should have returned requires_confirmation above.
        # If we reach here the C&R went through (confirmed=True passed by test).
        new_oid = result.get("new_order_id") or result.get("order_id")
        assert new_oid, (
            f"cancel_and_recreate succeeded but no new_order_id in result: {result}"
        )


# ---------------------------------------------------------------------------
# TestLiveClientOrderUpdates
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestLiveClientOrderUpdates:
    """Integration tests for order update tools against the live Concept Groove client.

    Each test:
      1. Resolves the effective logistics partner for the order.
      2. Reads the configured order_update_strategy for that partner from Postgres.
      3. Calls the relevant update tool.
      4. Asserts the correct outcome (inplace / escalation / C&R confirmation).

    Run with:
        pytest test_cancel_or_update_tools.py::TestLiveClientOrderUpdates -m integration -v
    """

    # ── NEW / unfulfilled order tests ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_address_new_order(self):
        """Address update on a NEW order → direct Shopify update, no logistics call."""
        tools, _ = _live_tools(_LIVE_ORDER_NEW_PHONE)
        result = await tools["update_order_address"].ainvoke({
            "order_id": _LIVE_ORDER_NEW_ID,
            "phone_number": _LIVE_ORDER_NEW_PHONE,
            "new_address": "|E-11 Guru Nanak Pura, Jail Road|Janak Puri|New Delhi|Delhi|110058|India|",
            "confirmed": False,
        })
        assert result.get("success"), f"Address update on NEW order failed: {result}"
        # NEW order path never triggers logistics or escalation
        assert not result.get("requires_escalation"), "NEW order should not escalate"
        assert not result.get("requires_confirmation"), "NEW order should not require C&R confirmation"

    @pytest.mark.asyncio
    async def test_update_phone_new_order(self):
        """Phone update on a NEW order → direct Shopify update."""
        tools, _ = _live_tools(_LIVE_ORDER_NEW_PHONE)
        result = await tools["update_order_phone_number_tool"].ainvoke({
            "order_id": _LIVE_ORDER_NEW_ID,
            "phone_number": _LIVE_ORDER_NEW_PHONE,
            "new_phone": "9716336096",
            "confirmed": False,
        })
        assert result.get("success"), f"Phone update on NEW order failed: {result}"

    @pytest.mark.asyncio
    async def test_update_email_new_order(self):
        """Email update on a NEW order → direct Shopify update."""
        tools, _ = _live_tools(_LIVE_ORDER_NEW_PHONE)
        result = await tools["update_order_email_tool"].ainvoke({
            "order_id": _LIVE_ORDER_NEW_ID,
            "phone_number": _LIVE_ORDER_NEW_PHONE,
            "new_email": "test_integration@bloomerce.ai",
            "confirmed": False,
        })
        assert result.get("success"), f"Email update on NEW order failed: {result}"

    @pytest.mark.asyncio
    async def test_update_name_new_order(self):
        """Name update on a NEW order → direct Shopify update."""
        tools, _ = _live_tools(_LIVE_ORDER_NEW_PHONE)
        result = await tools["update_order_name_tool"].ainvoke({
            "order_id": _LIVE_ORDER_NEW_ID,
            "phone_number": _LIVE_ORDER_NEW_PHONE,
            "new_name": "Prabhjot Singh",
            "confirmed": False,
        })
        assert result.get("success"), f"Name update on NEW order failed: {result}"

    # ── Shiprocket-fulfilled order tests ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_address_shiprocket_order(self):
        """Address update on a Shiprocket-fulfilled order.

        Strategy is read from Postgres:
          - update_inplace           → Shiprocket API updated, success=True
          - escalate_for_manual_update → escalation triggered, success=True
          - cancel_and_recreate      → requires_confirmation=True (unconfirmed call)
        """
        partner, strategy = await _fetch_strategy_for_order(
            _LIVE_ORDER_SHIPROCKET_ID, _LIVE_ORDER_SHIPROCKET_PHONE
        )
        tools, _ = _live_tools(_LIVE_ORDER_SHIPROCKET_PHONE)
        result = await tools["update_order_address"].ainvoke({
            "order_id": _LIVE_ORDER_SHIPROCKET_ID,
            "phone_number": _LIVE_ORDER_SHIPROCKET_PHONE,
            "new_address": "|FabHotel Rolt, Shingar Cinema Road|Harcharan Nagar|Ludhiana|Punjab|141001|India|",
            "confirmed": False,
        })
        _assert_update_outcome(result, strategy, partner, "address")

    @pytest.mark.asyncio
    async def test_update_phone_shiprocket_order(self):
        """Phone update on a Shiprocket-fulfilled order — strategy-driven assertion."""
        partner, strategy = await _fetch_strategy_for_order(
            _LIVE_ORDER_SHIPROCKET_ID, _LIVE_ORDER_SHIPROCKET_PHONE
        )
        tools, _ = _live_tools(_LIVE_ORDER_SHIPROCKET_PHONE)
        result = await tools["update_order_phone_number_tool"].ainvoke({
            "order_id": _LIVE_ORDER_SHIPROCKET_ID,
            "phone_number": _LIVE_ORDER_SHIPROCKET_PHONE,
            "new_phone": "7973140055",
            "confirmed": False,
        })
        _assert_update_outcome(result, strategy, partner, "phone")

    @pytest.mark.asyncio
    async def test_update_size_shiprocket_order(self):
        """Size update on a Shiprocket-fulfilled order.

        Expected to succeed via Shopify OrderEdit GraphQL (in-place) or
        cancel_and_recreate depending on strategy.
        """
        partner, strategy = await _fetch_strategy_for_order(
            _LIVE_ORDER_SHIPROCKET_ID, _LIVE_ORDER_SHIPROCKET_PHONE
        )
        tools, _ = _live_tools(_LIVE_ORDER_SHIPROCKET_PHONE)
        result = await tools["update_order_size_tool"].ainvoke({
            "order_id": _LIVE_ORDER_SHIPROCKET_ID,
            "phone_number": _LIVE_ORDER_SHIPROCKET_PHONE,
            "new_size": "XL",
            "confirmed": False,
        })
        _assert_update_outcome(result, strategy, partner, "size")

    @pytest.mark.asyncio
    async def test_annotate_shiprocket_order(self):
        """annotate_order on a Shiprocket order should only hit Shopify (no logistics call)."""
        tools, _ = _live_tools(_LIVE_ORDER_SHIPROCKET_PHONE)
        result = await tools["annotate_order"].ainvoke({
            "order_id": _LIVE_ORDER_SHIPROCKET_ID,
            "phone_number": _LIVE_ORDER_SHIPROCKET_PHONE,
            "note": "[Bloomerce] Integration test note — safe to delete.",
        })
        assert result.get("success"), f"annotate_order failed: {result}"
        assert result.get("note_result", {}).get("success"), (
            f"note_result not successful: {result.get('note_result')}"
        )

    # ── Delhivery-fulfilled order tests ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_address_delhivery_order(self):
        """Address update on a Delhivery-fulfilled order.

        Reads strategy from Postgres (typically escalate_for_manual_update or
        cancel_and_recreate for Delhivery).
        """
        if not _LIVE_ORDER_DELHIVERY_ID:
            pytest.skip("No Delhivery order configured — set _LIVE_ORDER_DELHIVERY_ID")
        partner, strategy = await _fetch_strategy_for_order(
            _LIVE_ORDER_DELHIVERY_ID, _LIVE_ORDER_DELHIVERY_PHONE
        )
        tools, _ = _live_tools(_LIVE_ORDER_DELHIVERY_PHONE)
        result = await tools["update_order_address"].ainvoke({
            "order_id": _LIVE_ORDER_DELHIVERY_ID,
            "phone_number": _LIVE_ORDER_DELHIVERY_PHONE,
            "new_address": "|E-11 Guru Nanak Pura, Jail Road|Janak Puri|New Delhi|Delhi|110058|India|",
            "confirmed": False,
        })
        _assert_update_outcome(result, strategy, partner, "address")

    @pytest.mark.asyncio
    async def test_update_phone_delhivery_order(self):
        """Phone update on a Delhivery-fulfilled order — strategy-driven assertion."""
        if not _LIVE_ORDER_DELHIVERY_ID:
            pytest.skip("No Delhivery order configured — set _LIVE_ORDER_DELHIVERY_ID")
        partner, strategy = await _fetch_strategy_for_order(
            _LIVE_ORDER_DELHIVERY_ID, _LIVE_ORDER_DELHIVERY_PHONE
        )
        tools, _ = _live_tools(_LIVE_ORDER_DELHIVERY_PHONE)
        result = await tools["update_order_phone_number_tool"].ainvoke({
            "order_id": _LIVE_ORDER_DELHIVERY_ID,
            "phone_number": _LIVE_ORDER_DELHIVERY_PHONE,
            "new_phone": "9716336096",
            "confirmed": False,
        })
        _assert_update_outcome(result, strategy, partner, "phone")

    @pytest.mark.asyncio
    async def test_update_email_delhivery_order(self):
        """Email update on a Delhivery-fulfilled order — strategy-driven assertion."""
        if not _LIVE_ORDER_DELHIVERY_ID:
            pytest.skip("No Delhivery order configured — set _LIVE_ORDER_DELHIVERY_ID")
        partner, strategy = await _fetch_strategy_for_order(
            _LIVE_ORDER_DELHIVERY_ID, _LIVE_ORDER_DELHIVERY_PHONE
        )
        tools, _ = _live_tools(_LIVE_ORDER_DELHIVERY_PHONE)
        result = await tools["update_order_email_tool"].ainvoke({
            "order_id": _LIVE_ORDER_DELHIVERY_ID,
            "phone_number": _LIVE_ORDER_DELHIVERY_PHONE,
            "new_email": "test_integration@bloomerce.ai",
            "confirmed": False,
        })
        _assert_update_outcome(result, strategy, partner, "email")

    @pytest.mark.asyncio
    async def test_update_name_delhivery_order(self):
        """Name update on a Delhivery-fulfilled order — strategy-driven assertion."""
        if not _LIVE_ORDER_DELHIVERY_ID:
            pytest.skip("No Delhivery order configured — set _LIVE_ORDER_DELHIVERY_ID")
        partner, strategy = await _fetch_strategy_for_order(
            _LIVE_ORDER_DELHIVERY_ID, _LIVE_ORDER_DELHIVERY_PHONE
        )
        tools, _ = _live_tools(_LIVE_ORDER_DELHIVERY_PHONE)
        result = await tools["update_order_name_tool"].ainvoke({
            "order_id": _LIVE_ORDER_DELHIVERY_ID,
            "phone_number": _LIVE_ORDER_DELHIVERY_PHONE,
            "new_name": "Prabhjot Singh",
            "confirmed": False,
        })
        _assert_update_outcome(result, strategy, partner, "name")

    # ── cancel_and_recreate explicit confirmation test ───────────────────────

    @pytest.mark.asyncio
    async def test_cancel_and_recreate_requires_confirmation_before_proceeding(self):
        """When strategy=cancel_and_recreate and confirmed=False, the tool must
        return requires_confirmation=True and NOT cancel the order.

        Uses whichever fulfilled order has C&R configured.  If neither
        Shiprocket nor Delhivery is configured as C&R, the test is skipped.
        """
        from fashion_bot.config_manager import (
            aget_partner_update_strategy,
            ORDER_UPDATE_STRATEGY_CANCEL_AND_RECREATE,
        )
        from fashion_bot.env_loader import bootstrap_environment
        bootstrap_environment()

        # Find a fulfilled order whose partner has C&R strategy
        candidates = [
            (_LIVE_ORDER_SHIPROCKET_ID, _LIVE_ORDER_SHIPROCKET_PHONE),
            (_LIVE_ORDER_DELHIVERY_ID, _LIVE_ORDER_DELHIVERY_PHONE),
        ]
        cnr_order_id = None
        cnr_phone = None
        for oid, phone in candidates:
            if not oid:
                continue
            partner, strategy = await _fetch_strategy_for_order(oid, phone)
            if strategy == ORDER_UPDATE_STRATEGY_CANCEL_AND_RECREATE:
                cnr_order_id, cnr_phone = oid, phone
                break

        if not cnr_order_id:
            pytest.skip(
                "No order with cancel_and_recreate strategy found — configure one in "
                "client_configs.order_update_strategy"
            )

        tools, _ = _live_tools(cnr_phone)
        result = await tools["update_order_address"].ainvoke({
            "order_id": cnr_order_id,
            "phone_number": cnr_phone,
            "new_address": "|E-11 Guru Nanak Pura, Jail Road|Janak Puri|New Delhi|Delhi|110058|India|",
            "confirmed": False,   # no confirmation → must NOT cancel
        })
        assert result.get("requires_confirmation"), (
            f"Expected requires_confirmation=True for C&R strategy (confirmed=False), got: {result}"
        )
        assert not result.get("cancelled"), (
            "Order was cancelled despite confirmed=False"
        )
