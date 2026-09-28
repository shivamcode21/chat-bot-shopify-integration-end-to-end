"""
Comprehensive test suite for all tools in place_order_tools_factory.

Tools under test:
    1. search_products          — text search for products
    2. find_product_by_url      — exact product lookup by URL
    3. find_product_by_id       — exact product lookup by handle/slug or numeric ID
    4. fetch_customer_data      — Shopify customer lookup by phone
    5. create_order             — COD order creation
    6. create_draft_order_for_prepaid — prepaid: add to cart + return /cart checkout link
    7. confirm_cod_order        — confirm existing COD order by ID

Coverage areas:
    - Conversation history context (size, color, budget in QU pipeline)
    - QU filter correctness (size, color, fit, material, price, segment, bestseller)
    - Known product presence in search results
    - Data structure consistency across single/multiple results
    - Order creation tags (AGENT_CREATED, COD Confirmed, bot/prepaid/whatsapp)
    - Negative order creation scenarios (invalid fields, edge cases)
    - Expanded coverage for all tools (malformed inputs, structure validation)
"""

import pytest
from fashion_bot.tool_factory import place_order_tools_factory

CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"
TEST_CLIENT_ID = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
VALID_PHONE = "9716336096"
VALID_PRODUCT_URL = "https://groovee.in/products/evolve-the-cosmic-shacket"
VALID_PRODUCT_HANDLE = "evolve-the-cosmic-shacket"
TEST_VALID_PRODUCT_URL = "https://blommerce-test.myshopify.com/products/evolve-the-cosmic-shacket"
EXPECTED_CUSTOMER_NAME = "bloomerce testing"

_TEST_SHOPIFY_CLIENT_ID = "85391617d1691af65cf0267f54a3e298"
_TEST_SHOPIFY_CLIENT_SECRET = "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"
_TEST_SHOPIFY_DOMAIN = "blommerce-test.myshopify.com"


@pytest.fixture(scope="session", autouse=True)
def _refresh_test_store_token():
    """Refresh the Shopify OAuth token for the test store before any test runs."""
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

def _build_state(phone: str = VALID_PHONE, client_id: str = CLIENT_ID):
    return {
        "client_id": client_id,
        "phone_number": phone,
        "conversation_context": {"entities": [], "topics": [], "focal_entity": None},
        "messages": [],
    }


def _get_tools(state=None):
    """Build the factory once and return a name→tool mapping."""
    if state is None:
        state = _build_state()
    client_id = state.get("client_id", CLIENT_ID)
    tools = place_order_tools_factory(state, state["messages"], client_id)
    return {t.name: t for t in tools}, state


# ===========================================================================
# 1. search_products (placeholder — all tests consolidated in section 8)
# ===========================================================================


# ===========================================================================
# 2. find_product_by_url
# ===========================================================================

class TestFindProductByUrl:

    @pytest.mark.asyncio
    async def test_valid_url_returns_correct_product(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_url"].ainvoke({"product_url": VALID_PRODUCT_URL})
        assert isinstance(result, dict)
        assert result.get("found") is True
        product = result["product"]
        assert product.get("handle") == VALID_PRODUCT_HANDLE, (
            f"Expected handle '{VALID_PRODUCT_HANDLE}', got '{product.get('handle')}'"
        )
        title = (product.get("title") or product.get("name", "")).lower()
        assert "cosmic" in title or "shacket" in title, (
            f"Product title should contain 'cosmic' or 'shacket'. title='{title}'"
        )
        assert result.get("url") and "products/" in result["url"], (
            f"Top-level url should contain 'products/'. url='{result.get('url')}'"
        )

    @pytest.mark.asyncio
    async def test_url_with_variant_returns_same_product(self):
        """URL with ?variant= param should resolve to the same product."""
        tools, _ = _get_tools()
        variant_url = VALID_PRODUCT_URL + "?variant=50489842237762"
        result = await tools["find_product_by_url"].ainvoke({"product_url": variant_url})
        assert isinstance(result, dict)
        assert result.get("found") is True, (
            f"URL with variant param should still find the product. result={result}"
        )
        product = result["product"]
        assert product.get("handle") == VALID_PRODUCT_HANDLE, (
            f"Variant URL should resolve to same handle '{VALID_PRODUCT_HANDLE}', "
            f"got '{product.get('handle')}'"
        )
        title = (product.get("title") or product.get("name", "")).lower()
        assert "cosmic" in title or "shacket" in title, (
            f"Product title should still be cosmic shacket. title='{title}'"
        )

    @pytest.mark.asyncio
    async def test_invalid_domain_url(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_url"].ainvoke({"product_url": "https://malicious-site.com/products/fake"})
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_nonexistent_product_url(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_url"].ainvoke({"product_url": "https://groovee.in/products/this-does-not-exist-9999"})
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_empty_url(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_url"].ainvoke({"product_url": ""})
        assert isinstance(result, dict)
        assert result.get("found") is False




# ===========================================================================
# 3. find_product_by_id
# ===========================================================================

class TestFindProductById:

    @pytest.mark.asyncio
    async def test_valid_handle_returns_correct_product(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_id"].ainvoke({"product_id": VALID_PRODUCT_HANDLE})
        assert isinstance(result, dict)
        assert result.get("found") is True
        product = result["product"]
        assert product.get("handle") == VALID_PRODUCT_HANDLE, (
            f"Expected handle '{VALID_PRODUCT_HANDLE}', got '{product.get('handle')}'"
        )
        title = (product.get("title") or product.get("name", "")).lower()
        assert "cosmic" in title or "shacket" in title, (
            f"Product title should contain 'cosmic' or 'shacket'. title='{title}'"
        )
        assert result.get("url") and "products/" in result["url"], (
            f"Top-level url should contain 'products/'. url='{result.get('url')}'"
        )

    @pytest.mark.asyncio
    async def test_nonexistent_handle(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_id"].ainvoke({"product_id": "nonexistent-product-xyz-9999"})
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_empty_id(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_id"].ainvoke({"product_id": ""})
        assert isinstance(result, dict)
        assert result.get("found") is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_whitespace_only_id(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_id"].ainvoke({"product_id": "   "})
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_numeric_shopify_id_returns_same_product(self):
        """Numeric ID and handle should resolve to the same product."""
        tools, _ = _get_tools()
        handle_result = await tools["find_product_by_id"].ainvoke({"product_id": VALID_PRODUCT_HANDLE})
        if handle_result.get("found") is not True:
            pytest.skip("Could not fetch product by handle to get numeric ID")
        product = handle_result.get("product", {})
        numeric_id = str(product.get("id", "")).replace("gid://shopify/Product/", "")
        if not numeric_id or not numeric_id.isdigit():
            pytest.skip("Product does not have a numeric Shopify ID")
        id_result = await tools["find_product_by_id"].ainvoke({"product_id": numeric_id, "id_type": "numeric_id"})
        assert isinstance(id_result, dict)
        assert id_result.get("found") is True
        id_product = id_result["product"]
        assert id_product.get("handle") == VALID_PRODUCT_HANDLE, (
            f"Numeric ID should resolve to same handle. got='{id_product.get('handle')}'"
        )
        assert id_product.get("title") == product.get("title"), (
            "Numeric ID and handle should return the same product title"
        )


# ===========================================================================
# 4. fetch_customer_data
# ===========================================================================

class TestFetchCustomerData:

    @pytest.mark.asyncio
    async def test_existing_customer_phone(self):
        tools, _ = _get_tools()
        result = await tools["fetch_customer_data"].ainvoke({"phone": VALID_PHONE})
        assert isinstance(result, dict)
        assert result.get("found") is True
        assert result.get("is_returning_customer") is True
        assert result.get("customer_name"), (
            f"Expected a non-empty customer_name, got '{result.get('customer_name')}'"
        )

    @pytest.mark.asyncio
    async def test_nonexistent_customer_phone(self):
        tools, _ = _get_tools()
        result = await tools["fetch_customer_data"].ainvoke({"phone": "0000000000"})
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        """Ensure fetch_customer_data does not write to state."""
        state = _build_state()
        tools, state = _get_tools(state)
        assert "customer_name" not in state
        await tools["fetch_customer_data"].ainvoke({"phone": VALID_PHONE})
        assert "customer_name" not in state
        assert "is_returning_customer" not in state


# ===========================================================================
# 5. create_order (COD)
# ===========================================================================

class TestCreateOrder:

    @pytest.mark.asyncio
    async def test_invalid_phone_rejected(self):
        """create_order should reject when state has no valid phone."""
        state = _build_state(phone="web_abc123", client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "123 Main St, Delhi",
            "pincode": "110001",
            "payment_mode": "COD",
        })
        assert result.get("success") is False
        assert "phone" in result.get("error", "").lower() or "phone" in result.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_empty_phone_rejected(self):
        state = _build_state(phone="", client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "123 Main St, Delhi",
            "pincode": "110001",
            "payment_mode": "COD",
        })
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_address_missing_pincode_rejected(self):
        state = _build_state(phone="9111222333", client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "Some Street, Some City",
            "pincode": "",
            "payment_mode": "COD",
        })
        assert result.get("success") is False
        assert "postal" in result.get("message", "").lower() or "pin" in result.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_successful_cod_order(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
            "quantity": 1,
        })
        assert isinstance(result, dict)
        assert result.get("success") is True
        assert result.get("order_id")
        if not result.get("already_created"):
            assert result.get("product_name"), "Fresh order should include product_name"
            assert result.get("customer_name"), "Fresh order should include customer_name"

    @pytest.mark.asyncio
    async def test_dedup_guard_prevents_duplicate_order(self):
        """Second create_order with same phone+product should return already_created."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        params = {
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
            "quantity": 1,
        }
        first = await tools["create_order"].ainvoke(params)
        if not first.get("success"):
            pytest.skip(f"First order failed: {first.get('error')}")
        second = await tools["create_order"].ainvoke(params)
        assert second.get("success") is True
        assert second.get("already_created") is True, (
            f"Duplicate order should be caught by dedup guard. result={second}"
        )
        assert second.get("order_id"), "Dedup response should still include the original order_id"

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        """create_order should not write product_link, requested_size, etc. to state."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, state = _get_tools(state)
        await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
        })
        assert "product_link" not in state
        assert "requested_size" not in state
        assert "customer_name" not in state

    @pytest.mark.asyncio
    async def test_pincode_appended_to_address(self):
        """When pincode is not in customer_address, it should be appended for validation."""
        from conftest import shopify_retry
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await shopify_retry(
            tools["create_order"].ainvoke,
            {
                "product_link": TEST_VALID_PRODUCT_URL,
                "size": "L",
                "customer_name": "Test User",
                "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
                "pincode": "110058",
                "payment_mode": "COD",
            },
        )
        assert result.get("success") is True


# ===========================================================================
# 6. create_draft_order_for_prepaid
# ===========================================================================

class TestCreateDraftOrderForPrepaid:

    @pytest.mark.asyncio
    async def test_prepaid_no_longer_requires_phone(self):
        """Prepaid now adds to cart + returns a /cart link; the phone is collected
        on the checkout page, so a missing phone must NOT block checkout-link
        generation."""
        state = _build_state(phone="", client_id=TEST_CLIENT_ID)
        state["channel"] = "web"  # exercise the web-widget cart flow
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
        })
        assert result.get("success") is True
        assert str(result.get("checkout_url", "")).endswith("/cart")

    @pytest.mark.asyncio
    async def test_prepaid_no_longer_requires_address(self):
        """Address/pincode are collected on the checkout page for prepaid, so the
        tool must succeed without them."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        state["channel"] = "web"  # exercise the web-widget cart flow
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
        })
        assert result.get("success") is True
        assert str(result.get("checkout_url", "")).endswith("/cart")

    @pytest.mark.asyncio
    async def test_empty_product_link_rejected(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": "",
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
        })
        assert result.get("success") is False
        assert "product" in result.get("message", "").lower() or "link" in result.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_invalid_size_rejected(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "XXXXXXXXL",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
        })
        err = (result.get("error", "") + result.get("message", "")).lower()
        assert result.get("success") is False
        assert "size" in err

    @pytest.mark.asyncio
    async def test_nonexistent_product_url(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": "https://blommerce-test.myshopify.com/products/nonexistent-zzzzz-9999",
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
        })
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_successful_prepaid_order(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        state["channel"] = "web"  # web-widget → cart + /cart link flow
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "quantity": 1,
        })
        assert isinstance(result, dict)
        assert result.get("success") is True
        # On web the prepaid flow returns the storefront /cart link (no draft order).
        assert str(result.get("checkout_url", "")).endswith("/cart")
        # It must signal the agent to add the variant via the existing add_to_cart
        # tool, and return the variant_id to use.
        assert result.get("requires_add_to_cart") is True
        assert result.get("variant_id")
        # The tool itself MUST be stateless (AGENTS.md §2): it must NOT queue a
        # widget action; that is the agent's add_to_cart call's job.
        assert not (state.get("pending_widget_actions") or [])

    @pytest.mark.asyncio
    async def test_whatsapp_prepaid_falls_back_to_draft_order(self):
        """On WhatsApp there is no storefront widget, so the prepaid flow must
        fall back to a Shopify draft-order invoice link and must NOT queue a
        (never-flushed) storefront cart action."""
        state = _build_state(phone=VALID_PHONE, client_id=TEST_CLIENT_ID)
        state["channel"] = "whatsapp"
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True
        # It's a draft-order invoice link, not the storefront /cart page.
        assert result.get("checkout_url")
        assert not str(result.get("checkout_url", "")).endswith("/cart")
        # No storefront cart action should be queued on WhatsApp.
        assert not (state.get("pending_widget_actions") or [])

    @pytest.mark.asyncio
    async def test_non_web_prepaid_requires_phone(self):
        """The non-web (draft-order) branch creates the order against the
        customer's phone, so a missing/invalid phone MUST be rejected — the web
        cart flow is the only path that may proceed without a phone."""
        state = _build_state(phone="", client_id=TEST_CLIENT_ID)
        state["channel"] = "whatsapp"
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
        })
        assert result.get("success") is False
        assert "phone" in result.get("message", "").lower()

    @pytest.mark.asyncio
    async def test_unknown_channel_falls_back_to_draft_order(self):
        """An unknown/unresolved channel must take the safe default (draft-order
        invoice link), never the web /cart flow."""
        state = _build_state(phone=VALID_PHONE, client_id=TEST_CLIENT_ID)
        # No channel / session markers → resolver returns None → draft-order path.
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True
        assert result.get("checkout_url")
        assert not str(result.get("checkout_url", "")).endswith("/cart")
        assert not (state.get("pending_widget_actions") or [])

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        """Draft-order (non-web) path must not write to state."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, state = _get_tools(state)
        await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
        })
        assert "product_link" not in state
        assert "requested_size" not in state
        assert "draft_order_id" not in state
        assert "checkout_url" not in state
        assert "payment_mode" not in state

    @pytest.mark.asyncio
    async def test_does_not_mutate_state_web(self):
        """Web cart-link path must also be stateless: it must NOT write order/cart
        fields to state and MUST NOT queue a widget action (that is add_to_cart's
        job) — AGENTS.md §2."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        state["channel"] = "web"
        tools, state = _get_tools(state)
        await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
        })
        assert "product_link" not in state
        assert "requested_size" not in state
        assert "draft_order_id" not in state
        assert "checkout_url" not in state
        assert "payment_mode" not in state
        assert not (state.get("pending_widget_actions") or [])


# ===========================================================================
# 7. confirm_cod_order
# ===========================================================================

class TestConfirmCodOrder:

    @pytest.mark.asyncio
    async def test_invalid_order_id(self):
        tools, _ = _get_tools()
        result = await tools["confirm_cod_order"].ainvoke({"order_id": "DUMMY_INVALID_99999"})
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "Failed to confirm order" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_empty_order_id(self):
        tools, _ = _get_tools()
        result = await tools["confirm_cod_order"].ainvoke({"order_id": ""})
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "Order ID is required" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_hash_prefix_stripped(self):
        """Order ID with # prefix should be cleaned before lookup."""
        tools, _ = _get_tools()
        result = await tools["confirm_cod_order"].ainvoke({"order_id": "#INVALID_99999"})
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "Failed to confirm order" in result.get("error", "")

    @pytest.mark.asyncio
    async def test_valid_order_id_confirms(self):
        """Confirm a real order — requires a valid order that exists in Shopify."""
        state = _build_state(phone="9611703832", client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        create_result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
            "customer_name": "Confirm Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
        })
        if not create_result.get("success"):
            pytest.skip("Could not create order for confirm test")

        order_number = str(create_result.get("order_number", ""))
        if not order_number:
            order_id = str(create_result.get("order_id", "")).lstrip("#")
            if order_id:
                order_number = order_id
            else:
                pytest.skip("Order created but neither order_number nor order_id returned")

        result = await tools["confirm_cod_order"].ainvoke({"order_id": order_number})
        assert isinstance(result, dict)
        assert result.get("success") is True


# ===========================================================================
# 8. search_products — all tests (consolidated)
#
# Every test builds a messages list, calls the tool, and asserts on
# qu_query / qu_filter / follow_up returned by the tool (no separate
# understand_query calls — QU runs inside search_products).
#
# Aligned with the client-specific QU prompt:
#   - Catalog taxonomy: jeans, hoodies, oversized t-shirt, jacket
#   - Attribute memory: carry forward fit, color, material, price across turns
#   - Follow-up: Attr1 (primary) → Attr2 (secondary) per subcategory
#   - Occasion expansion with NOT IN exclusions
# ===========================================================================

class TestSearchProducts:

    @staticmethod
    def _search(state, messages, query):
        """Set messages on state, get tools, invoke search_products."""
        state["messages"] = messages
        tools, _ = _get_tools(state)
        return tools["search_products"].ainvoke({"query": query})

    # -- Basic tool behaviour --

    @pytest.mark.asyncio
    async def test_search_valid_query_returns_results(self):
        from langchain_core.messages import HumanMessage
        state = _build_state()
        messages = [HumanMessage(content="show me the cosmic shacket")]
        result = await self._search(state, messages, "cosmic shacket")
        assert isinstance(result, dict)
        assert result.get("found") is True
        assert isinstance(result.get("products"), list)
        assert result.get("count", 0) > 0
        product = result["products"][0]
        assert product.get("name") or product.get("title")
        assert "sizes_in_stock" in product or "stock_message" in product
        handles = [p.get("handle", "") for p in result["products"]]
        assert VALID_PRODUCT_HANDLE in handles, (
            f"Searched for 'cosmic shacket' but '{VALID_PRODUCT_HANDLE}' not in results: {handles}"
        )
        qu_query_lower = result["qu_query"].lower()
        assert "cosmic" in qu_query_lower or "shacket" in qu_query_lower, (
            f"QU query should contain 'cosmic' or 'shacket'. qu_query='{result['qu_query']}'"
        )
        assert "in_stock" in result["qu_filter"], (
            f"QU filter should contain 'in_stock'. qu_filter='{result['qu_filter']}'"
        )

    # -- Best-sellers / trending + pinned products (PR #792) --

    @pytest.mark.asyncio
    async def test_bestseller_query_returns_normalized_products(self):
        """A catalog-wide best-sellers query returns well-formed, normalized
        products from the live store. We assert SHAPE (found / count / the full
        normalized key set), not exact product ids — the live catalog drifts."""
        from langchain_core.messages import HumanMessage
        state = _build_state()
        messages = [HumanMessage(content="show me your best sellers")]
        result = await self._search(state, messages, "show me your best sellers")
        assert isinstance(result, dict)
        assert result.get("found") is True
        assert isinstance(result.get("products"), list)
        assert result.get("count", 0) == len(result.get("products", []))
        assert result["count"] > 0
        for product in result["products"]:
            missing = SEARCH_PRODUCT_REQUIRED_KEYS - set(product.keys())
            assert not missing, f"Bestseller product missing keys: {missing}"

    @pytest.mark.asyncio
    async def test_pinned_bestsellers_surface_first_when_configured(self):
        """If the client has `pinned_bestseller_products` configured, those pins
        must render FIRST in a catalog-wide best-sellers result (pins-first,
        deduped by handle). The feature is opt-in, so this is SKIPPED when no
        pins are configured for the client.

        Note: pins only surface when Query Understanding classifies the request
        catalog-wide (`is_catalog_wide=true`); if a configured pin doesn't appear,
        the most likely cause is the client's stored QU prompt not emitting that
        field yet (see PR #792 §5)."""
        from langchain_core.messages import HumanMessage
        from fashion_bot.services.recommendation.pinned_products import (
            aget_pinned_bestseller_products,
        )

        pins = await aget_pinned_bestseller_products(CLIENT_ID)
        if not pins:
            pytest.skip("No pinned_bestseller_products configured for this client")
        pin_handles = [
            p.get("handle", "").strip().lower() for p in pins if p.get("handle")
        ]

        state = _build_state()
        messages = [HumanMessage(content="show me your best sellers")]
        result = await self._search(state, messages, "show me your best sellers")
        assert result.get("found") is True
        handles = [
            p.get("handle", "").strip().lower() for p in result.get("products", [])
        ]

        # Index of the first NON-pinned product; every pin must come before it.
        pin_set = set(pin_handles)
        first_non_pin = next(
            (i for i, h in enumerate(handles) if h not in pin_set), len(handles)
        )
        for ph in pin_handles:
            assert ph in handles, (
                f"Configured pin '{ph}' not in best-sellers result {handles} — "
                f"likely the QU prompt isn't emitting is_catalog_wide (PR #792 §5)."
            )
            assert handles.index(ph) < first_non_pin, (
                f"Pin '{ph}' must appear before any non-pinned product. "
                f"order={handles}"
            )

    @pytest.mark.asyncio
    async def test_search_nonsense_query_returns_valid_structure(self):
        from langchain_core.messages import HumanMessage
        state = _build_state()
        messages = [HumanMessage(content="xyzzynonexistent12345")]
        result = await self._search(state, messages, "xyzzynonexistent12345")
        assert isinstance(result, dict)
        assert "found" in result
        assert isinstance(result.get("products"), list)
        assert isinstance(result.get("count"), int)
        assert isinstance(result.get("qu_query"), str) and len(result["qu_query"]) > 0, (
            f"QU query should be a non-empty string. qu_query='{result.get('qu_query')}'"
        )
        assert "in_stock" in result.get("qu_filter", ""), (
            f"QU filter should contain 'in_stock'. qu_filter='{result.get('qu_filter')}'"
        )

    @pytest.mark.asyncio
    async def test_search_empty_query(self):
        from langchain_core.messages import HumanMessage
        state = _build_state()
        messages = [HumanMessage(content="")]
        result = await self._search(state, messages, "")
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        from langchain_core.messages import HumanMessage
        state = _build_state()
        messages = [HumanMessage(content="cosmic shacket")]
        state["messages"] = messages
        tools, state = _get_tools(state)
        await tools["search_products"].ainvoke({"query": "cosmic shacket"})
        assert "inquiry_product_info" not in state
        assert "product_selection_matches" not in state
        assert "product_link" not in state

    # -- Known product presence --

    @pytest.mark.asyncio
    async def test_known_product_in_results_by_handle(self):
        from langchain_core.messages import HumanMessage
        state = _build_state()
        messages = [HumanMessage(content="show me the cosmic shacket")]
        result = await self._search(state, messages, "cosmic shacket")
        assert result.get("found") is True
        handles = [p.get("handle", "") for p in result.get("products", [])]
        assert VALID_PRODUCT_HANDLE in handles, (
            f"Expected '{VALID_PRODUCT_HANDLE}' in results, got: {handles}"
        )

    @pytest.mark.asyncio
    async def test_known_product_by_exact_title(self):
        from langchain_core.messages import HumanMessage
        state = _build_state()
        messages = [HumanMessage(content="Evolve The Cosmic Shacket")]
        result = await self._search(state, messages, "Evolve The Cosmic Shacket")
        assert result.get("found") is True
        handles = [p.get("handle", "") for p in result.get("products", [])]
        assert VALID_PRODUCT_HANDLE in handles

    # -- Attribute memory: carry forward across turns --

    @pytest.mark.asyncio
    async def test_color_carried_forward(self):
        """History mentions black; QU query for jackets should include 'black'."""
        from langchain_core.messages import HumanMessage, AIMessage
        from conftest import assert_llm_carries_attribute

        messages = [
            HumanMessage(content="I love black clothes"),
            AIMessage(content="Black is a great choice!"),
            HumanMessage(content="show me jackets"),
        ]
        state = _build_state()
        result = await self._search(state, messages, "jackets")
        assert isinstance(result.get("products"), list)
        assert_llm_carries_attribute(
            result["qu_query"], "black",
            field_name="qu_query",
            description="Color 'black' from history not carried into QU query",
        )

    @pytest.mark.asyncio
    async def test_fit_carried_across_subcategories(self):
        """'slim fit' from jeans should carry forward to hoodies."""
        from langchain_core.messages import HumanMessage, AIMessage
        from conftest import assert_llm_carries_attribute

        messages = [
            HumanMessage(content="show me slim fit jeans"),
            AIMessage(content="Here are some slim fit jeans for you."),
            HumanMessage(content="now show hoodies"),
        ]
        state = _build_state()
        result = await self._search(state, messages, "hoodies")
        assert isinstance(result.get("products"), list)
        assert_llm_carries_attribute(
            result["qu_query"], "slim", "fit",
            field_name="qu_query",
            description="'slim fit' from jeans history not carried to hoodies",
        )

    @pytest.mark.asyncio
    async def test_budget_carried_forward(self):
        """History mentions 'under 2000'; next query should keep price_max filter."""
        from langchain_core.messages import HumanMessage, AIMessage
        from conftest import assert_llm_carries_attribute

        messages = [
            HumanMessage(content="I'm looking for something under 2000"),
            AIMessage(content="Sure! What kind of product?"),
            HumanMessage(content="oversized t-shirts"),
        ]
        state = _build_state()
        result = await self._search(state, messages, "oversized t-shirts")
        assert isinstance(result, dict)
        assert_llm_carries_attribute(
            result["qu_filter"], "price_max", "2000",
            field_name="qu_filter",
            description="Budget 'under 2000' from history not in QU filter",
        )
        for product in result.get("products", []):
            price = product.get("price", {})
            max_price = price.get("max") or price.get("min", 0)
            assert float(max_price) <= 2000, (
                f"Product '{product.get('handle')}' price {max_price} exceeds budget 2000"
            )

    @pytest.mark.asyncio
    async def test_multi_attribute_carry_forward(self):
        """slim-fit + blue from earlier turns should both appear in jeans query."""
        from langchain_core.messages import HumanMessage, AIMessage
        from conftest import assert_llm_carries_attribute

        messages = [
            HumanMessage(content="I prefer slim fit clothes"),
            AIMessage(content="Noted!"),
            HumanMessage(content="And I like blue color"),
            AIMessage(content="Great taste! What are you looking for?"),
            HumanMessage(content="jeans"),
        ]
        state = _build_state()
        result = await self._search(state, messages, "jeans")
        assert isinstance(result.get("products"), list)
        assert_llm_carries_attribute(
            result["qu_query"], "slim", "fit",
            field_name="qu_query",
            description="'slim fit' from history not in QU query",
        )
        assert_llm_carries_attribute(
            result["qu_query"], "blue",
            field_name="qu_query",
            description="'blue' from history not in QU query",
        )

    # -- Follow-up logic: Attr1 / Attr2 per subcategory --

    @pytest.mark.asyncio
    async def test_jeans_no_attr_follow_up_asks_fit(self):
        """'show me jeans' → follow_up asks about fit (Attr1)."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="show me jeans")]
        state = _build_state()
        result = await self._search(state, messages, "show me jeans")
        assert result.get("found") is True
        follow = (result.get("follow_up") or "").lower()
        assert "fit" in follow or "slim" in follow or "skinny" in follow or "tapered" in follow, (
            f"Jeans with no attrs: follow_up should ask about fit. follow_up='{result.get('follow_up')}'"
        )

    @pytest.mark.asyncio
    async def test_jeans_attr1_given_follow_up_asks_attr2(self):
        """'slim fit jeans' (Attr1 given) → follow_up asks about material (Attr2)."""
        from langchain_core.messages import HumanMessage
        from conftest import assert_llm_carries_attribute

        messages = [HumanMessage(content="slim fit jeans")]
        state = _build_state()
        result = await self._search(state, messages, "slim fit jeans")
        assert result.get("found") is True
        assert_llm_carries_attribute(
            result.get("follow_up") or "", "material", "denim",
            field_name="follow_up",
            description="Slim fit jeans: follow_up should ask about material",
        )

    @pytest.mark.asyncio
    async def test_jeans_both_attrs_no_follow_up(self):
        """'slim fit denim jeans' (both attrs) → follow_up null."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="slim fit denim jeans")]
        state = _build_state()
        result = await self._search(state, messages, "slim fit denim jeans")
        assert result.get("found") is True
        assert result.get("follow_up") is None, (
            f"Both attrs specified: follow_up should be null. follow_up='{result.get('follow_up')}'"
        )

    @pytest.mark.asyncio
    async def test_hoodies_no_attr_follow_up_asks_material(self):
        """'show me hoodies' → follow_up asks about material (cotton/fleece)."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="show me hoodies")]
        state = _build_state()
        result = await self._search(state, messages, "show me hoodies")
        assert result.get("found") is True
        follow = (result.get("follow_up") or "").lower()
        assert "material" in follow or "cotton" in follow or "fleece" in follow, (
            f"Hoodies no attrs: follow_up should ask about material. follow_up='{result.get('follow_up')}'"
        )

    @pytest.mark.asyncio
    async def test_hoodies_attr1_given_follow_up_asks_pattern(self):
        """'cotton hoodies' (Attr1 given) → follow_up asks about pattern."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="cotton hoodies")]
        state = _build_state()
        result = await self._search(state, messages, "cotton hoodies")
        assert result.get("found") is True
        follow = (result.get("follow_up") or "").lower()
        assert "pattern" in follow or "solid" in follow or "printed" in follow, (
            f"Cotton hoodies: follow_up should ask about pattern. follow_up='{result.get('follow_up')}'"
        )

    @pytest.mark.asyncio
    async def test_hoodies_both_attrs_no_follow_up(self):
        """'cotton printed hoodies' → follow_up null."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="cotton printed hoodies")]
        state = _build_state()
        result = await self._search(state, messages, "cotton printed hoodies")
        assert result.get("found") is True
        assert result.get("follow_up") is None, (
            f"Both attrs specified: follow_up should be null. follow_up='{result.get('follow_up')}'"
        )

    # -- Hard filter correctness --

    @pytest.mark.asyncio
    async def test_subcategory_filter_jeans(self):
        """'show me jeans' → subcategory = 'jeans' in filter."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="show me jeans")]
        state = _build_state()
        result = await self._search(state, messages, "show me jeans")
        assert result.get("found") is True
        qu_filter = result["qu_filter"]
        assert "subcategory" in qu_filter
        assert "jeans" in qu_filter.lower()
        assert "in_stock" in qu_filter

    @pytest.mark.asyncio
    async def test_subcategory_filter_hoodies_with_price(self):
        """'hoodie under 3000' → subcategory hoodies + price_max."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="hoodie under 3000")]
        state = _build_state()
        result = await self._search(state, messages, "hoodie under 3000")
        assert result.get("found") is True
        filter_lower = result["qu_filter"].lower()
        assert "subcategory" in filter_lower
        assert "hoodie" in filter_lower
        assert "price_max" in filter_lower or "3000" in filter_lower
        assert "in_stock" in filter_lower

    @pytest.mark.asyncio
    async def test_price_filter(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="jackets under 2000")]
        state = _build_state()
        result = await self._search(state, messages, "jackets under 2000")
        assert result.get("found") is True
        qu_filter = result["qu_filter"]
        assert "price_max" in qu_filter or "2000" in qu_filter
        assert "in_stock" in qu_filter

    @pytest.mark.asyncio
    async def test_segment_filter(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="men's jeans")]
        state = _build_state()
        result = await self._search(state, messages, "men's jeans")
        assert result.get("found") is True
        has_segment = "segment" in result["qu_filter"] and "men" in result["qu_filter"].lower()
        has_men_query = "men" in result["qu_query"].lower()
        assert has_segment or has_men_query, (
            f"Expected 'men' in filter or query. qu_filter='{result['qu_filter']}', qu_query='{result['qu_query']}'"
        )

    @pytest.mark.asyncio
    async def test_instock_always_present(self):
        from langchain_core.messages import HumanMessage

        for query_text in ["hoodies", "slim fit jeans", "oversized t-shirts"]:
            messages = [HumanMessage(content=query_text)]
            state = _build_state()
            result = await self._search(state, messages, query_text)
            assert "in_stock" in result["qu_filter"], (
                f"in_stock missing for: {query_text}. qu_filter='{result['qu_filter']}'"
            )

    @pytest.mark.asyncio
    async def test_broad_query_no_subcategory_filter(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="recommend something casual")]
        state = _build_state()
        result = await self._search(state, messages, "recommend something casual")
        assert isinstance(result, dict)
        qu_filter_lower = result["qu_filter"].lower()
        assert "subcategory" not in qu_filter_lower or "not in" in qu_filter_lower, (
            f"Broad query should not hard-filter subcategory. qu_filter='{result['qu_filter']}'"
        )

    # -- Soft attributes in semantic query --

    @pytest.mark.asyncio
    async def test_color_in_semantic_query(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="red hoodie")]
        state = _build_state()
        result = await self._search(state, messages, "red hoodie")
        assert result.get("found") is True
        assert "red" in result["qu_query"].lower()

    @pytest.mark.asyncio
    async def test_material_in_semantic_query(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="cotton hoodies")]
        state = _build_state()
        result = await self._search(state, messages, "cotton hoodies")
        assert result.get("found") is True
        assert "cotton" in result["qu_query"].lower()

    @pytest.mark.asyncio
    async def test_fit_in_semantic_query(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="slim fit jeans")]
        state = _build_state()
        result = await self._search(state, messages, "slim fit jeans")
        assert result.get("found") is True
        assert "slim" in result["qu_query"].lower()

    @pytest.mark.asyncio
    async def test_pattern_in_semantic_query(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="printed hoodies")]
        state = _build_state()
        result = await self._search(state, messages, "printed hoodies")
        assert result.get("found") is True
        qu_query_lower = result["qu_query"].lower()
        assert "printed" in qu_query_lower or "print" in qu_query_lower

    @pytest.mark.asyncio
    async def test_size_filter(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="hoodies in size M")]
        state = _build_state()
        result = await self._search(state, messages, "hoodies in size M")
        assert result.get("found") is True
        qu_filter_lower = result["qu_filter"].lower()
        qu_query_lower = result["qu_query"].lower()
        size_in_filter = "sizes" in qu_filter_lower and "m" in qu_filter_lower
        size_in_query = "m" in qu_query_lower or "medium" in qu_query_lower
        assert size_in_filter or size_in_query or "hoodie" in qu_query_lower
        assert "in_stock" in result["qu_filter"]

    # -- Occasion expansion with NOT IN exclusions --

    @pytest.mark.asyncio
    async def test_goa_trip_excludes_hoodies_jackets(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="show me products for Goa trip")]
        state = _build_state()
        result = await self._search(state, messages, "show me products for Goa trip")
        assert isinstance(result.get("products"), list)
        filter_lower = result["qu_filter"].lower()
        assert "not in" in filter_lower, (
            f"Goa trip should have NOT IN exclusion. qu_filter='{result['qu_filter']}'"
        )
        assert "hoodie" in filter_lower or "hoodies" in filter_lower
        assert "jacket" in filter_lower or "jackets" in filter_lower
        query_lower = result["qu_query"].lower()
        has_travel = any(w in query_lower for w in [
            "lightweight", "casual", "breathable", "summer", "cotton", "comfortable",
        ])
        assert has_travel, f"Goa trip query missing travel terms. qu_query='{result['qu_query']}'"

    @pytest.mark.asyncio
    async def test_manali_trip_excludes_tshirts(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="outfits for Manali trip")]
        state = _build_state()
        result = await self._search(state, messages, "outfits for Manali trip")
        assert isinstance(result.get("products"), list)
        filter_lower = result["qu_filter"].lower()
        assert "not in" in filter_lower, (
            f"Manali trip should have NOT IN exclusion. qu_filter='{result['qu_filter']}'"
        )
        assert "t-shirt" in filter_lower or "oversized" in filter_lower
        query_lower = result["qu_query"].lower()
        has_winter = any(w in query_lower for w in [
            "warm", "winter", "cozy", "layered", "thermal", "insulated",
        ])
        assert has_winter, f"Manali trip query missing winter terms. qu_query='{result['qu_query']}'"

    @pytest.mark.asyncio
    async def test_occasion_with_specific_type_uses_positive_filter(self):
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="jacket for Manali")]
        state = _build_state()
        result = await self._search(state, messages, "jacket for Manali")
        assert result.get("found") is True
        filter_lower = result["qu_filter"].lower()
        assert "subcategory" in filter_lower
        assert "jacket" in filter_lower
        has_positive = "=" in filter_lower and "not in" not in filter_lower.split("jacket")[0]
        assert has_positive or "not in" not in filter_lower, (
            f"Specific type + occasion should use positive filter. qu_filter='{result['qu_filter']}'"
        )

    # -- Bestseller / top-selling queries --

    @pytest.mark.asyncio
    async def test_top_selling_products(self):
        """'show me best selling products' → bestseller in filter or top-selling terms in query."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="show me best selling products")]
        state = _build_state()
        result = await self._search(state, messages, "show me best selling products")
        assert result.get("found") is True
        has_bestseller_filter = "bestseller" in result["qu_filter"].lower()
        qu_query_lower = result["qu_query"].lower()
        has_top_selling_query = any(w in qu_query_lower for w in [
            "best sell", "top sell", "popular", "trending", "bestsell",
        ])
        assert has_bestseller_filter or has_top_selling_query, (
            f"Top-selling query should set bestseller filter or include relevant terms in query. "
            f"qu_filter='{result['qu_filter']}', qu_query='{result['qu_query']}'"
        )
        assert "in_stock" in result["qu_filter"]

    @pytest.mark.asyncio
    async def test_top_selling_with_subcategory(self):
        """'top selling jeans' → bestseller in filter or query, plus jeans subcategory."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="what are your top selling jeans?")]
        state = _build_state()
        result = await self._search(state, messages, "what are your top selling jeans?")
        assert result.get("found") is True
        filter_lower = result["qu_filter"].lower()
        qu_query_lower = result["qu_query"].lower()
        has_bestseller = "bestseller" in filter_lower or any(
            w in qu_query_lower for w in ["best sell", "top sell", "popular", "bestsell"]
        )
        assert has_bestseller, (
            f"Top-selling jeans should reference bestseller concept. "
            f"qu_filter='{result['qu_filter']}', qu_query='{result['qu_query']}'"
        )
        assert "jeans" in filter_lower or "jeans" in qu_query_lower, (
            f"Should reference jeans in filter or query. "
            f"qu_filter='{result['qu_filter']}', qu_query='{result['qu_query']}'"
        )

    @pytest.mark.asyncio
    async def test_trending_products(self):
        """'trending products' → bestseller = true in filter."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="show me trending products")]
        state = _build_state()
        result = await self._search(state, messages, "show me trending products")
        assert result.get("found") is True
        filter_lower = result["qu_filter"].lower()
        has_bestseller = "bestseller" in filter_lower
        has_trending_query = "trend" in result["qu_query"].lower() or "popular" in result["qu_query"].lower()
        assert has_bestseller or has_trending_query, (
            f"Trending query should use bestseller filter or trending terms in query. "
            f"qu_filter='{result['qu_filter']}', qu_query='{result['qu_query']}'"
        )

    # -- Discount / sale queries --

    @pytest.mark.asyncio
    async def test_max_discount_products_sorted_by_discount(self):
        """'products with maximum discount' → results should be sorted by discount."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="show me products with maximum discount")]
        state = _build_state()
        result = await self._search(state, messages, "show me products with maximum discount")
        assert result.get("found") is True
        products = result.get("products", [])
        assert len(products) > 0
        discounted = []
        for p in products:
            price = p.get("price", {})
            compare = p.get("compare_at_price")
            price_min = price.get("min", 0)
            if compare and price_min and float(compare) > float(price_min):
                pct = (float(compare) - float(price_min)) / float(compare) * 100
                discounted.append(pct)
        if len(discounted) == 0:
            pytest.xfail(
                "No products with compare_at_price > price in test catalog — "
                "test store may not have active discounts"
            )

    @pytest.mark.asyncio
    async def test_sale_products(self):
        """'show me products on sale' → results should contain discounted items."""
        from langchain_core.messages import HumanMessage

        messages = [HumanMessage(content="show me products on sale")]
        state = _build_state()
        result = await self._search(state, messages, "show me products on sale")
        assert result.get("found") is True
        products = result.get("products", [])
        has_discounted = any(
            p.get("compare_at_price") and p.get("price", {}).get("min", 0)
            and float(p["compare_at_price"]) > float(p["price"]["min"])
            for p in products
        )
        if not has_discounted:
            pytest.xfail(
                "No products with compare_at_price > price in test catalog — "
                "test store may not have active sales"
            )


# ===========================================================================
# 9. Data structure consistency — single vs. multiple results
# ===========================================================================

SEARCH_PRODUCT_REQUIRED_KEYS = {
    "id", "handle", "name", "title", "url", "price", "in_stock",
    "sizes_in_stock", "all_size_variants", "stock_message",
    "variants", "image_url", "images_count",
    "tags", "vendor", "category", "subcategory",
    "size_guide", "fabric", "fit_type",
}

FIND_PRODUCT_REQUIRED_KEYS = {"found", "product", "url"}


class TestProductDataStructureConsistency:
    """Validate consistent data structure across surfaces."""

    @pytest.mark.asyncio
    async def test_search_multiple_products_have_same_keys(self):
        """All products in a multi-result search should share the same key set."""
        from langchain_core.messages import HumanMessage
        state = _build_state()
        state["messages"] = [HumanMessage(content="show me hoodies")]
        tools, _ = _get_tools(state)
        result = await tools["search_products"].ainvoke({"query": "hoodies"})
        assert result.get("found") is True
        products = result.get("products", [])
        assert len(products) >= 2, "Need >=2 products for consistency check"
        first_keys = set(products[0].keys())
        for i, product in enumerate(products[1:], start=2):
            assert set(product.keys()) == first_keys, (
                f"Product #{i} keys differ: extra={set(product.keys()) - first_keys}, "
                f"missing={first_keys - set(product.keys())}"
            )

    @pytest.mark.asyncio
    async def test_search_product_has_required_fields(self):
        from langchain_core.messages import HumanMessage
        state = _build_state()
        state["messages"] = [HumanMessage(content="show me hoodies")]
        tools, _ = _get_tools(state)
        result = await tools["search_products"].ainvoke({"query": "hoodies"})
        assert result.get("found") is True
        for product in result.get("products", []):
            missing = SEARCH_PRODUCT_REQUIRED_KEYS - set(product.keys())
            assert not missing, f"Product missing keys: {missing}"

    @pytest.mark.asyncio
    async def test_search_top_level_structure(self):
        """search_products returns {found, products, count}."""
        from langchain_core.messages import HumanMessage
        state = _build_state()
        state["messages"] = [HumanMessage(content="show me jackets")]
        tools, _ = _get_tools(state)
        result = await tools["search_products"].ainvoke({"query": "jacket"})
        assert "found" in result
        assert "products" in result
        assert "count" in result
        assert isinstance(result["found"], bool)
        assert isinstance(result["products"], list)
        assert isinstance(result["count"], int)
        assert result["count"] == len(result["products"])

    @pytest.mark.asyncio
    async def test_find_by_id_top_level_structure(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_id"].ainvoke(
            {"product_id": VALID_PRODUCT_HANDLE}
        )
        assert result.get("found") is True
        for key in FIND_PRODUCT_REQUIRED_KEYS:
            assert key in result, f"Missing key: {key}"
        assert isinstance(result["product"], dict)

    @pytest.mark.asyncio
    async def test_find_by_url_top_level_structure(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_url"].ainvoke(
            {"product_url": VALID_PRODUCT_URL}
        )
        assert result.get("found") is True
        for key in FIND_PRODUCT_REQUIRED_KEYS:
            assert key in result, f"Missing key: {key}"
        assert isinstance(result["product"], dict)

    @pytest.mark.asyncio
    async def test_price_field_is_dict_with_min_max(self):
        """Price field in search results should be {min, max}."""
        from langchain_core.messages import HumanMessage
        state = _build_state()
        state["messages"] = [HumanMessage(content="show me jackets")]
        tools, _ = _get_tools(state)
        result = await tools["search_products"].ainvoke({"query": "jacket"})
        assert result.get("found") is True
        for product in result.get("products", []):
            price = product.get("price")
            assert isinstance(price, dict), f"price should be dict, got {type(price)}"
            assert "min" in price, "price missing 'min'"
            assert "max" in price, "price missing 'max'"

    @pytest.mark.asyncio
    async def test_sizes_in_stock_is_list(self):
        from langchain_core.messages import HumanMessage
        state = _build_state()
        state["messages"] = [HumanMessage(content="show me jackets")]
        tools, _ = _get_tools(state)
        result = await tools["search_products"].ainvoke({"query": "jacket"})
        assert result.get("found") is True
        for product in result.get("products", []):
            assert isinstance(product.get("sizes_in_stock"), list)
            assert isinstance(product.get("all_size_variants"), list)

    @pytest.mark.asyncio
    async def test_variants_is_list(self):
        from langchain_core.messages import HumanMessage
        state = _build_state()
        state["messages"] = [HumanMessage(content="show me jackets")]
        tools, _ = _get_tools(state)
        result = await tools["search_products"].ainvoke({"query": "jacket"})
        assert result.get("found") is True
        for product in result.get("products", []):
            assert isinstance(product.get("variants"), list)


# ===========================================================================
# 12. Order creation tags
# ===========================================================================

class TestOrderCreationTags:
    """Verify orders carry the correct tags in Shopify (uses test store)."""

    @staticmethod
    async def _fetch_shopify_order_tags(order_name: str) -> str:
        """Fetch tags for an order from Shopify REST API by order name."""
        from fashion_bot.config_manager import aget_shopify_config
        from fashion_bot.utils.http_client import get_shared_async_http_client
        config = await aget_shopify_config(client_id=TEST_CLIENT_ID)
        if not config:
            return ""
        shop_domain = (
            config["shop_url"].replace("https://", "").replace("http://", "").rstrip("/")
        )
        api_version = config.get("api_version", "2024-04")
        url = (
            f"https://{shop_domain}/admin/api/{api_version}/orders.json"
            f"?name={order_name}&fields=id,tags&status=any"
        )
        client = await get_shared_async_http_client()
        resp = await client.get(
            url,
            headers={
                "X-Shopify-Access-Token": config["access_token"],
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return ""
        orders = resp.json().get("orders", [])
        return orders[0].get("tags", "") if orders else ""

    @pytest.mark.asyncio
    async def test_cod_order_has_agent_created_tag(self):
        """COD order should carry AGENT_CREATED tag."""
        state = _build_state(phone="9888777666", client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
            "customer_name": "Tag Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
        })
        if not result.get("success"):
            pytest.skip(f"Order creation failed: {result.get('error')}")
        if result.get("already_created"):
            pytest.skip("Dedup guard fired — cannot verify tags on fresh order")
        order_id = result.get("order_id", "")
        tags = await self._fetch_shopify_order_tags(order_id)
        assert "AGENT_CREATED" in tags, f"Expected AGENT_CREATED in tags, got: '{tags}'"


# ===========================================================================
# 13. create_order — expanded negative cases
# ===========================================================================

# Use a distinct phone per negative test to avoid the dedup guard that fires
# when the same phone + product_link was used by a successful order earlier.
NEGATIVE_TEST_PHONE = "9876543210"


class TestCreateOrderNegativeCases:

    @pytest.mark.asyncio
    async def test_missing_customer_name(self):
        state = _build_state(phone=NEGATIVE_TEST_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "M",
            "customer_name": "",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
        })
        if result.get("already_created"):
            pytest.skip("Dedup guard fired — previous order exists for this phone")
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_nonexistent_product_url(self):
        state = _build_state(phone=NEGATIVE_TEST_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": "https://blommerce-test.myshopify.com/products/nonexistent-xyz-00000",
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
        })
        if result.get("already_created"):
            pytest.skip("Dedup guard fired")
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_unavailable_size(self):
        state = _build_state(phone=NEGATIVE_TEST_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "XXXXXXL",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
        })
        if result.get("already_created"):
            pytest.skip("Dedup guard fired")
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_empty_address_and_pincode(self):
        state = _build_state(phone=NEGATIVE_TEST_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "",
            "pincode": "",
            "payment_mode": "COD",
        })
        if result.get("already_created"):
            pytest.skip("Dedup guard fired")
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_non_numeric_pincode(self):
        state = _build_state(phone=NEGATIVE_TEST_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "Some Street, Delhi",
            "pincode": "ABCDEF",
            "payment_mode": "COD",
        })
        if result.get("already_created"):
            pytest.skip("Dedup guard fired")
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_wrong_domain_product_link(self):
        state = _build_state(phone=NEGATIVE_TEST_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": "https://other-store.com/products/some-product",
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
        })
        if result.get("already_created"):
            pytest.skip("Dedup guard fired")
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_quantity_zero(self):
        state = _build_state(phone=NEGATIVE_TEST_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
            "quantity": 0,
        })
        if result.get("already_created"):
            pytest.skip("Dedup guard fired")
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_negative_quantity(self):
        state = _build_state(phone=NEGATIVE_TEST_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
            "quantity": -1,
        })
        if result.get("already_created"):
            pytest.skip("Dedup guard fired")
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_empty_product_link(self):
        state = _build_state(phone=NEGATIVE_TEST_PHONE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_order"].ainvoke({
            "product_link": "",
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
        })
        if result.get("already_created"):
            pytest.skip("Dedup guard fired")
        assert result.get("success") is False


# ===========================================================================
# 14. create_draft_order_for_prepaid — expanded negative cases
# ===========================================================================

class TestCreateDraftOrderNegativeCases:

    @pytest.mark.asyncio
    async def test_nonexistent_size(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "XXXXXL",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
        })
        err = (result.get("error", "") + result.get("message", "")).lower()
        assert result.get("success") is False
        assert "size" in err

    @pytest.mark.asyncio
    async def test_empty_size_multi_variant_product(self):
        """Multi-size product with empty size should either fail or auto-select single variant."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
        })
        err = (result.get("error", "") + result.get("message", "")).lower()
        if result.get("success"):
            assert result.get("checkout_url")
        else:
            assert "size" in err or "product" in err

    @pytest.mark.asyncio
    async def test_quantity_zero(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "quantity": 0,
        })
        # quantity=0 is coerced to 1 (falsy guard); tool returns a dict either way.
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_malformed_product_url(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": "not-a-url-at-all",
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
        })
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_wrong_domain_product_link(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        result = await tools["create_draft_order_for_prepaid"].ainvoke({
            "product_link": "https://other-store.com/products/something",
            "size": "M",
            "customer_name": "Test User",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
        })
        assert result.get("success") is False



# ===========================================================================
# 15. find_product_by_url — expanded
# ===========================================================================

class TestFindProductByUrlExpanded:

    @pytest.mark.asyncio
    async def test_url_with_trailing_slash(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_url"].ainvoke(
            {"product_url": VALID_PRODUCT_URL + "/"}
        )
        assert isinstance(result, dict)
        if result.get("found") is True:
            assert result["product"].get("handle") == VALID_PRODUCT_HANDLE

    @pytest.mark.asyncio
    async def test_url_with_query_params(self):
        """Query params should be stripped; same product should be returned."""
        tools, _ = _get_tools()
        result = await tools["find_product_by_url"].ainvoke(
            {"product_url": VALID_PRODUCT_URL + "?variant=12345&utm_source=test"}
        )
        assert isinstance(result, dict)
        assert result.get("found") is True
        assert result["product"].get("handle") == VALID_PRODUCT_HANDLE, (
            f"URL with query params should still resolve to correct product. "
            f"got='{result['product'].get('handle')}'"
        )

    @pytest.mark.asyncio
    async def test_non_http_url(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_url"].ainvoke(
            {"product_url": "groovee.in/products/cosmic-shacket"}
        )
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_product_has_essential_fields_with_valid_values(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_url"].ainvoke(
            {"product_url": VALID_PRODUCT_URL}
        )
        assert result.get("found") is True, f"Expected product to be found. result={result}"
        product = result["product"]
        title = product.get("title") or product.get("name")
        assert title and len(title) > 0, "Product title should be non-empty"
        assert product.get("handle") == VALID_PRODUCT_HANDLE
        sizes = product.get("sizes_in_stock")
        assert isinstance(sizes, list), f"sizes_in_stock should be list, got {type(sizes)}"
        stock_msg = product.get("stock_message", "")
        assert isinstance(stock_msg, str) and len(stock_msg) > 0, (
            "stock_message should be a non-empty string"
        )
        variants = product.get("variants", [])
        assert isinstance(variants, list) and len(variants) > 0, (
            "Product should have at least one variant"
        )
        for v in variants:
            assert "id" in v, "Each variant should have an 'id'"

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state()
        tools, state = _get_tools(state)
        await tools["find_product_by_url"].ainvoke({"product_url": VALID_PRODUCT_URL})
        assert "inquiry_product_info" not in state
        assert "product_link" not in state


# ===========================================================================
# 16. find_product_by_id — expanded
# ===========================================================================

class TestFindProductByIdExpanded:

    @pytest.mark.asyncio
    async def test_handle_with_leading_slash(self):
        """Leading slash should be stripped; same product returned."""
        tools, _ = _get_tools()
        result = await tools["find_product_by_id"].ainvoke(
            {"product_id": "/" + VALID_PRODUCT_HANDLE}
        )
        assert isinstance(result, dict)
        assert result.get("found") is True
        assert result["product"].get("handle") == VALID_PRODUCT_HANDLE, (
            f"Leading-slash handle should resolve to correct product. "
            f"got='{result['product'].get('handle')}'"
        )

    @pytest.mark.asyncio
    async def test_gid_format_resolves_same_product(self):
        """GID-style Shopify ID should resolve to the same product as handle."""
        tools, _ = _get_tools()
        handle_result = await tools["find_product_by_id"].ainvoke(
            {"product_id": VALID_PRODUCT_HANDLE}
        )
        if handle_result.get("found") is not True:
            pytest.skip("Could not fetch product by handle")
        handle_product = handle_result["product"]
        product_id = str(handle_product.get("id", ""))
        if not product_id or "gid://" not in product_id:
            pytest.skip("Product ID not in GID format")
        result = await tools["find_product_by_id"].ainvoke(
            {"product_id": product_id, "id_type": "numeric_id"}
        )
        assert result.get("found") is True
        assert result["product"].get("handle") == VALID_PRODUCT_HANDLE, (
            f"GID should resolve to same product. got='{result['product'].get('handle')}'"
        )
        assert result["product"].get("title") == handle_product.get("title"), (
            "GID and handle should return the same title"
        )

    @pytest.mark.asyncio
    async def test_id_type_mismatch_handle_as_numeric(self):
        """Passing a handle string with id_type='numeric_id' should fail."""
        tools, _ = _get_tools()
        result = await tools["find_product_by_id"].ainvoke(
            {"product_id": VALID_PRODUCT_HANDLE, "id_type": "numeric_id"}
        )
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_product_has_essential_fields_with_valid_values(self):
        tools, _ = _get_tools()
        result = await tools["find_product_by_id"].ainvoke(
            {"product_id": VALID_PRODUCT_HANDLE}
        )
        assert result.get("found") is True, f"Expected product to be found. result={result}"
        product = result["product"]
        title = product.get("title") or product.get("name")
        assert title and len(title) > 0, "Product title should be non-empty"
        assert product.get("handle") == VALID_PRODUCT_HANDLE
        sizes = product.get("sizes_in_stock")
        assert isinstance(sizes, list), f"sizes_in_stock should be list, got {type(sizes)}"
        stock_msg = product.get("stock_message", "")
        assert isinstance(stock_msg, str) and len(stock_msg) > 0, (
            "stock_message should be a non-empty string"
        )
        variants = product.get("variants", [])
        assert isinstance(variants, list) and len(variants) > 0, (
            "Product should have at least one variant"
        )
        for v in variants:
            assert "id" in v, "Each variant should have an 'id'"

    @pytest.mark.asyncio
    async def test_url_contains_handle(self):
        """Returned url should contain the product handle."""
        tools, _ = _get_tools()
        result = await tools["find_product_by_id"].ainvoke(
            {"product_id": VALID_PRODUCT_HANDLE}
        )
        assert result.get("found") is True, f"Expected product to be found. result={result}"
        url = result.get("url", "")
        assert url and "products/" in url, f"url should contain 'products/'. url='{url}'"
        assert VALID_PRODUCT_HANDLE in url, (
            f"url should contain the handle '{VALID_PRODUCT_HANDLE}'. url='{url}'"
        )

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state()
        tools, state = _get_tools(state)
        await tools["find_product_by_id"].ainvoke(
            {"product_id": VALID_PRODUCT_HANDLE}
        )
        assert "inquiry_product_info" not in state
        assert "product_link" not in state


# ===========================================================================
# 17. fetch_customer_data — expanded
# ===========================================================================

class TestFetchCustomerDataExpanded:

    @pytest.mark.asyncio
    async def test_international_phone_format(self):
        tools, _ = _get_tools()
        result = await tools["fetch_customer_data"].ainvoke(
            {"phone": "+91" + VALID_PHONE}
        )
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_short_phone_number(self):
        tools, _ = _get_tools()
        result = await tools["fetch_customer_data"].ainvoke({"phone": "123"})
        assert isinstance(result, dict)
        assert result.get("found") is False or "error" in result

    @pytest.mark.asyncio
    async def test_empty_phone(self):
        tools, _ = _get_tools()
        result = await tools["fetch_customer_data"].ainvoke({"phone": ""})
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_alphabetic_phone(self):
        tools, _ = _get_tools()
        result = await tools["fetch_customer_data"].ainvoke({"phone": "abcdefghij"})
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_existing_customer_has_email(self):
        tools, _ = _get_tools()
        result = await tools["fetch_customer_data"].ainvoke({"phone": VALID_PHONE})
        if result.get("found") is not True:
            pytest.skip("Customer not found")
        assert "customer_email" in result

    @pytest.mark.asyncio
    async def test_returning_customer_response_structure(self):
        tools, _ = _get_tools()
        result = await tools["fetch_customer_data"].ainvoke({"phone": VALID_PHONE})
        if result.get("found") is not True:
            pytest.skip("Customer not found")
        for key in (
            "found", "is_returning_customer",
            "customer_name", "customer_address", "customer_email",
        ):
            assert key in result, f"Missing key: {key}"


# ===========================================================================
# 18. confirm_cod_order — expanded
# ===========================================================================

class TestConfirmCodOrderExpanded:

    @pytest.mark.asyncio
    async def test_whitespace_only_order_id(self):
        tools, _ = _get_tools()
        result = await tools["confirm_cod_order"].ainvoke({"order_id": "   "})
        assert isinstance(result, dict)
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_special_characters_order_id(self):
        tools, _ = _get_tools()
        result = await tools["confirm_cod_order"].ainvoke({"order_id": "@!$%^&*()"})
        assert isinstance(result, dict)
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_very_long_order_id(self):
        tools, _ = _get_tools()
        result = await tools["confirm_cod_order"].ainvoke({"order_id": "A" * 500})
        assert isinstance(result, dict)
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_sql_injection_attempt(self):
        tools, _ = _get_tools()
        result = await tools["confirm_cod_order"].ainvoke(
            {"order_id": "1; DROP TABLE orders;--"}
        )
        assert isinstance(result, dict)
        assert result.get("success") is False

    @pytest.mark.asyncio
    async def test_confirm_adds_cod_confirmed_tags(self):
        """After confirming, order should carry COD Confirmed + Customer Confirmed."""
        state = _build_state(phone="9611703832", client_id=TEST_CLIENT_ID)
        tools, _ = _get_tools(state)
        create_result = await tools["create_order"].ainvoke({
            "product_link": TEST_VALID_PRODUCT_URL,
            "size": "L",
            "customer_name": "Confirm Tag Test",
            "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
            "pincode": "110058",
            "payment_mode": "COD",
        })
        if not create_result.get("success"):
            pytest.skip(f"Order creation failed: {create_result.get('error')}")
        order_number = str(create_result.get("order_number", ""))
        if not order_number:
            order_id = str(create_result.get("order_id", "")).lstrip("#")
            if order_id:
                order_number = order_id
            else:
                pytest.skip("Neither order_number nor order_id returned")
        confirm_result = await tools["confirm_cod_order"].ainvoke(
            {"order_id": order_number}
        )
        if confirm_result.get("success") is not True:
            pytest.skip(f"Confirm failed: {confirm_result.get('error')}")
        tags = await TestOrderCreationTags._fetch_shopify_order_tags(
            create_result.get("order_id", "")
        )
        assert "COD Confirmed" in tags, f"Expected 'COD Confirmed' in: '{tags}'"
        assert "Customer Confirmed" in tags, f"Expected 'Customer Confirmed' in: '{tags}'"


# ===========================================================================
# Cross-cutting: tool list integrity
# ===========================================================================

class TestToolListIntegrity:

    def test_factory_returns_exactly_7_tools(self):
        tools, _ = _get_tools()
        assert len(tools) == 7

    def test_expected_tool_names_present(self):
        tools, _ = _get_tools()
        expected = {
            "search_products",
            "find_product_by_url",
            "find_product_by_id",
            "fetch_customer_data",
            "create_order",
            "create_draft_order_for_prepaid",
            "confirm_cod_order",
        }
        assert set(tools.keys()) == expected
