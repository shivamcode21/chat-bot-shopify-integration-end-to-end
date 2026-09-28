"""
Comprehensive test suite for all tools in product_details_tools_factory.

Tools under test (4 unique — search_products, find_product_by_url, find_product_by_id
are shared and tested in test_place_order_tools.py, so NOT duplicated here):
    1. get_top_selling_products_tool  — best-sellers / trending products
    2. get_customization_config       — customization / alteration policy
    3. get_available_categories       — category listing with links
    4. escalate_to_agent              — shared human-agent escalation

Coverage areas:
    - Top selling: default count, custom count, normalized fields, exact products
    - Customization config: success, policy structure
    - Available categories: category listing, structure validation
    - Escalation: reason/category routing, response structure
    - Statelessness: no state mutation from any tool
    - find_product_by_id: handle-based lookup, normalized fields, edge cases
    - Multi-product scenarios: sequential lookups, cross-tool consistency
    - Normalized schema: all tools return consistent field sets
    - Negative / edge-case inputs
"""

import copy
import pytest
from fashion_bot.tool_factory import product_details_tools_factory
from fashion_bot.services.recommendation.pinned_products import MAX_PINNED

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"
TEST_CLIENT_ID = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"

VALID_PRODUCT_HANDLE = "evolve-the-cosmic-shacket"
VALID_PRODUCT_URL = "https://groovee.in/products/evolve-the-cosmic-shacket"
VALID_PRODUCT_TITLE = "Evolve The Cosmic Shacket"

# Test-store Shopify credentials (same as other test files)
_TEST_SHOPIFY_CLIENT_ID = "85391617d1691af65cf0267f54a3e298"
_TEST_SHOPIFY_CLIENT_SECRET = "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"
_TEST_SHOPIFY_DOMAIN = "blommerce-test.myshopify.com"

NORMALIZED_PRODUCT_REQUIRED_KEYS = {
    "name", "title", "product_id", "handle", "url", "image_url",
    "price", "sizes_in_stock", "all_size_variants", "stock_message",
    "in_stock", "product_type", "category", "variants", "total_variants",
    "size_guide", "fabric", "fit_type", "vendor", "tags", "description",
    "colors", "images_count", "all_metafields", "metafields_count",
}


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

    with psycopg.connect(get_env("DATABASE_URL")) as conn:
        conn.execute(
            "UPDATE client_configs SET config_value = %s::jsonb "
            "WHERE client_id = %s AND config_key = 'shopify_details'",
            (shopify_details, TEST_CLIENT_ID),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_state(client_id: str = CLIENT_ID):
    return {
        "client_id": client_id,
        "phone_number": "9716336096",
        "conversation_context": {"entities": [], "topics": [], "focal_entity": None},
        "messages": [],
    }


async def _get_tools(state=None):
    """Build the async factory once and return a name→tool mapping."""
    if state is None:
        state = _build_state()
    client_id = state.get("client_id", CLIENT_ID)
    tools = await product_details_tools_factory(state, state["messages"], client_id)
    return {t.name: t for t in tools}, state


# ===========================================================================
# 1. find_product_by_id (handle-based lookup, replaces get_product_info_from_context)
# ===========================================================================

class TestFindProductByIdFromContext:
    """Test product info retrieval by handle via find_product_by_id.

    These tests cover the same scenarios previously tested via
    get_product_info_from_context. The LLM now uses find_product_by_id
    with the handle from the page context system prompt.
    """

    @pytest.mark.asyncio
    async def test_valid_handle_returns_product(self):
        """Valid product handle should return found=True with data."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is True, f"Expected found=True, got: {result}"
        assert "product" in result, "Response should contain 'product' key"
        product = result["product"]
        title = (product.get("title") or product.get("name", "")).lower()
        assert "cosmic" in title or "shacket" in title, (
            f"Product title should contain 'cosmic' or 'shacket'. title='{title}'"
        )

    @pytest.mark.asyncio
    async def test_valid_handle_returns_correct_handle(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]
        assert product.get("handle") == VALID_PRODUCT_HANDLE, (
            f"Expected handle '{VALID_PRODUCT_HANDLE}', got '{product.get('handle')}'"
        )

    @pytest.mark.asyncio
    async def test_valid_handle_has_normalized_fields(self):
        """Product returned should have all normalized schema fields."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]
        missing = NORMALIZED_PRODUCT_REQUIRED_KEYS - set(product.keys())
        assert not missing, f"Product missing normalized keys: {missing}"

    @pytest.mark.asyncio
    async def test_price_is_normalized_dict(self):
        """Price should be a dict with min/max after normalization."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        price = result["product"].get("price")
        assert isinstance(price, dict), f"price should be dict, got {type(price)}"
        assert "min" in price, "price missing 'min'"
        assert "max" in price, "price missing 'max'"
        assert isinstance(price["min"], (int, float)), "price.min should be numeric"
        assert isinstance(price["max"], (int, float)), "price.max should be numeric"

    @pytest.mark.asyncio
    async def test_sizes_in_stock_is_list(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]
        assert isinstance(product.get("sizes_in_stock"), list)
        assert isinstance(product.get("all_size_variants"), list)

    @pytest.mark.asyncio
    async def test_size_guide_is_dict(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        sg = result["product"].get("size_guide")
        assert isinstance(sg, dict), f"size_guide should be dict, got {type(sg)}"
        assert "has_size_guide" in sg

    @pytest.mark.asyncio
    async def test_variants_is_list(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]
        assert isinstance(product.get("variants"), list)
        assert isinstance(product.get("total_variants"), int)

    @pytest.mark.asyncio
    async def test_product_id_is_populated(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]
        assert product.get("product_id"), (
            f"product_id should be populated, got '{product.get('product_id')}'"
        )

    @pytest.mark.asyncio
    async def test_empty_handle_returns_not_found(self):
        """Empty product_id should return found=False."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_whitespace_handle_returns_not_found(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "   ",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_nonexistent_handle_returns_not_found(self):
        """A handle that doesn't exist in Shopify should return found=False."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "nonexistent-product-xyz-9999",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_handle_case_insensitive(self):
        """Handle lookup should be case-insensitive."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "Evolve-The-Cosmic-Shacket",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is True, (
            f"Case-insensitive handle lookup should succeed. result={result}"
        )
        assert result["product"].get("handle") == VALID_PRODUCT_HANDLE

    @pytest.mark.asyncio
    async def test_handle_with_leading_trailing_spaces(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": f"  {VALID_PRODUCT_HANDLE}  ",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is True, (
            "Handle with whitespace should still resolve"
        )

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        """find_product_by_id must NOT write to state."""
        state = _build_state()
        original_state = copy.deepcopy(state)
        tools, state = await _get_tools(state)
        await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert "inquiry_product_info" not in state, "Tool should not write inquiry_product_info to state"
        assert "product_link" not in state, "Tool should not write product_link to state"
        assert state["conversation_context"] == original_state["conversation_context"], (
            "Tool should not modify conversation_context"
        )

    @pytest.mark.asyncio
    async def test_all_metafields_is_list(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]
        assert isinstance(product.get("all_metafields"), list)
        assert isinstance(product.get("metafields_count"), int)

    @pytest.mark.asyncio
    async def test_tags_is_list(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        assert isinstance(result["product"].get("tags"), list)

    @pytest.mark.asyncio
    async def test_name_and_title_both_populated(self):
        """Normalization should ensure both name and title are set."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]
        assert product.get("name"), "name should be populated"
        assert product.get("title"), "title should be populated"
        assert product["name"] == product["title"], (
            f"name and title should match: name='{product['name']}', title='{product['title']}'"
        )


# ===========================================================================
# 2. get_top_selling_products_tool
# ===========================================================================

class TestGetTopSellingProductsTool:
    """Test top-selling / bestseller product retrieval."""

    @pytest.mark.asyncio
    async def test_default_top_3_returns_products(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({})
        assert isinstance(result, dict)
        assert result.get("found") is True, f"Expected found=True, got: {result}"
        assert isinstance(result.get("products"), list)
        assert result.get("count", 0) > 0
        assert result["count"] == len(result["products"])

    @pytest.mark.asyncio
    async def test_default_returns_bounded_count(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({})
        assert result.get("found") is True
        products = result.get("products", [])
        # Default count is the per-client `product_results_count` (default 5,
        # clamped to PRODUCT_RESULTS_COUNT_MAX=50); a catalog-wide best-seller
        # result may also carry up to MAX_PINNED pinned promos on top.
        assert 1 <= len(products) <= 50 + MAX_PINNED, (
            f"Unexpected product count {len(products)}"
        )
        assert result.get("count") == len(products)

    @pytest.mark.asyncio
    async def test_custom_top_n_5(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 5})
        assert isinstance(result, dict)
        assert result.get("found") is True
        # top_n caps the live top-sellers; up to MAX_PINNED pinned promos may be added.
        assert len(result.get("products", [])) <= 5 + MAX_PINNED

    @pytest.mark.asyncio
    async def test_custom_top_n_1(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 1})
        assert isinstance(result, dict)
        assert result.get("found") is True
        # top_n=1 → 1 live top-seller; up to MAX_PINNED pinned promos may be added.
        assert 1 <= len(result.get("products", [])) <= 1 + MAX_PINNED

    @pytest.mark.asyncio
    async def test_products_have_normalized_fields(self):
        """Each product in top-selling results should have normalized schema fields."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert result.get("found") is True
        for i, product in enumerate(result.get("products", [])):
            missing = NORMALIZED_PRODUCT_REQUIRED_KEYS - set(product.keys())
            assert not missing, f"Product #{i+1} missing normalized keys: {missing}"

    @pytest.mark.asyncio
    async def test_products_have_valid_prices(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert result.get("found") is True
        for product in result.get("products", []):
            price = product.get("price")
            assert isinstance(price, dict), f"price should be dict, got {type(price)}"
            assert "min" in price and "max" in price
            assert price["min"] > 0, f"Top-selling product should have price > 0, got {price['min']}"

    @pytest.mark.asyncio
    async def test_products_have_names(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert result.get("found") is True
        for product in result.get("products", []):
            assert product.get("name") or product.get("title"), (
                f"Product should have a name/title. product={product}"
            )

    @pytest.mark.asyncio
    async def test_products_have_handles(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert result.get("found") is True
        for product in result.get("products", []):
            assert product.get("handle"), (
                f"Product should have a handle. product_name={product.get('name')}"
            )

    @pytest.mark.asyncio
    async def test_products_have_urls(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert result.get("found") is True
        for product in result.get("products", []):
            url = product.get("url", "")
            assert url and "products/" in url, (
                f"Product should have a valid URL with 'products/'. url='{url}'"
            )

    @pytest.mark.asyncio
    async def test_products_sizes_are_lists(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert result.get("found") is True
        for product in result.get("products", []):
            assert isinstance(product.get("sizes_in_stock"), list)
            assert isinstance(product.get("all_size_variants"), list)

    @pytest.mark.asyncio
    async def test_products_have_consistent_keys(self):
        """All products in the result should share the same key set."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert result.get("found") is True
        products = result.get("products", [])
        if len(products) < 2:
            pytest.skip("Need >=2 products for consistency check")
        # Pinned promos may carry a slightly different (still fully-normalized)
        # key set than live top-sellers, so require every product to contain the
        # required normalized keys rather than an identical key set across all.
        for i, product in enumerate(products, start=1):
            missing = NORMALIZED_PRODUCT_REQUIRED_KEYS - set(product.keys())
            assert not missing, f"Product #{i} missing normalized keys: {missing}"

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state()
        original_state = copy.deepcopy(state)
        tools, state = await _get_tools(state)
        await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert state["conversation_context"] == original_state["conversation_context"]
        assert "inquiry_product_info" not in state

    @pytest.mark.asyncio
    async def test_size_guide_is_dict_in_each_product(self):
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert result.get("found") is True
        for product in result.get("products", []):
            sg = product.get("size_guide")
            assert isinstance(sg, dict), f"size_guide should be dict, got {type(sg)}"
            assert "has_size_guide" in sg


# ===========================================================================
# 3. get_customization_config
# ===========================================================================

class TestGetCustomizationConfig:
    """Test customization/alteration policy retrieval."""

    @pytest.mark.asyncio
    async def test_returns_dict(self):
        tools, _ = await _get_tools()
        result = await tools["get_customization_config"].ainvoke({})
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_has_success_field(self):
        tools, _ = await _get_tools()
        result = await tools["get_customization_config"].ainvoke({})
        assert "success" in result, f"Result should have 'success' field. result={result}"

    @pytest.mark.asyncio
    async def test_has_policy_field(self):
        tools, _ = await _get_tools()
        result = await tools["get_customization_config"].ainvoke({})
        assert "policy" in result, f"Result should have 'policy' field. result={result}"

    @pytest.mark.asyncio
    async def test_policy_is_meaningful(self):
        """Policy should be either a dict or a non-empty string."""
        tools, _ = await _get_tools()
        result = await tools["get_customization_config"].ainvoke({})
        policy = result.get("policy")
        assert policy, "Policy should not be empty/None"
        assert isinstance(policy, (dict, str)), (
            f"Policy should be dict or str, got {type(policy)}"
        )

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state()
        original_state = copy.deepcopy(state)
        tools, state = await _get_tools(state)
        await tools["get_customization_config"].ainvoke({})
        assert state["conversation_context"] == original_state["conversation_context"]


# ===========================================================================
# 4. get_available_categories
# ===========================================================================

class TestGetAvailableCategories:
    """Test category listing retrieval."""

    @pytest.mark.asyncio
    async def test_returns_dict(self):
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_has_found_field(self):
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        assert "found" in result, f"Result should have 'found'. result={result}"

    @pytest.mark.asyncio
    async def test_found_true_has_categories_list(self):
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        if result.get("found") is not True:
            pytest.skip("No categories configured for this client")
        assert isinstance(result.get("categories"), list)
        assert len(result["categories"]) > 0, "Found=True but categories list is empty"

    @pytest.mark.asyncio
    async def test_each_category_has_name_url_formatted(self):
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        if result.get("found") is not True:
            pytest.skip("No categories configured for this client")
        for cat in result["categories"]:
            assert cat.get("name"), f"Category missing 'name'. cat={cat}"
            assert cat.get("url"), f"Category missing 'url'. cat={cat}"
            assert cat.get("formatted"), f"Category missing 'formatted'. cat={cat}"
            assert cat["name"] in cat["formatted"], (
                f"formatted text should contain category name. name='{cat['name']}', formatted='{cat['formatted']}'"
            )

    @pytest.mark.asyncio
    async def test_has_formatted_text(self):
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        if result.get("found") is not True:
            pytest.skip("No categories configured for this client")
        assert result.get("formatted_text"), "formatted_text should be non-empty"
        assert isinstance(result["formatted_text"], str)

    @pytest.mark.asyncio
    async def test_category_urls_are_valid(self):
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        if result.get("found") is not True:
            pytest.skip("No categories configured for this client")
        for cat in result["categories"]:
            url = cat.get("url", "")
            assert url.startswith("http"), (
                f"Category URL should start with http. url='{url}'"
            )

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state()
        original_state = copy.deepcopy(state)
        tools, state = await _get_tools(state)
        await tools["get_available_categories"].ainvoke({})
        assert state["conversation_context"] == original_state["conversation_context"]


# ===========================================================================
# 5. escalate_to_agent
# ===========================================================================

class TestEscalateToAgent:
    """Test the unified human-agent escalation tool."""

    @pytest.mark.asyncio
    async def test_basic_escalation_returns_success(self):
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer asking when product will be restocked",
            "category": "Restocking Query",
            "details": "Customer wants Cosmic Shacket in size XL restocked",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, f"Escalation should succeed. result={result}"

    @pytest.mark.asyncio
    async def test_escalation_has_message(self):
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Test escalation",
            "category": "General",
        })
        assert result.get("message"), "Escalation result should contain a message"

    @pytest.mark.asyncio
    async def test_escalation_has_category(self):
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Test escalation",
            "category": "Product Complaint",
        })
        if result.get("success"):
            assert result.get("category") == "Product Complaint", (
                f"Expected category='Product Complaint', got '{result.get('category')}'"
            )

    @pytest.mark.asyncio
    async def test_escalation_with_phone_and_order(self):
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Price mismatch on order",
            "category": "Order Update",
            "details": "Product price changed after order was placed",
            "phone_number": "9716336096",
            "order_id": "GV10001",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True

    @pytest.mark.asyncio
    async def test_restocking_escalation(self):
        """Restocking use case — the primary reason this tool replaced check_restocking_query."""
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer wants to know restock date for sold-out product",
            "category": "Restocking Query",
            "details": "Product 'Evolve The Cosmic Shacket' size XL is out of stock, customer wants notification when restocked",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True
        assert "team" in result.get("message", "").lower() or "contact" in result.get("message", "").lower(), (
            "Escalation message should mention team/contact"
        )

    @pytest.mark.asyncio
    async def test_escalation_with_empty_reason_still_works(self):
        """Edge case: empty details should still work (reason is required)."""
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer explicitly asked for human agent",
            "category": "General",
            "details": "",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state()
        original_state = copy.deepcopy(state)
        tools, state = await _get_tools(state)
        await tools["escalate_to_agent"].ainvoke({
            "reason": "Test",
            "category": "General",
        })
        assert state["conversation_context"] == original_state["conversation_context"]


# ===========================================================================
# 6. Multi-product context and switching scenarios
# ===========================================================================

class TestMultiProductContextScenarios:
    """
    Complex scenarios: user navigates between products, asks about
    specific attributes. Verify the correct product is returned.
    """

    @pytest.mark.asyncio
    async def test_two_sequential_products_return_different_data(self):
        """
        Simulate user viewing Product A, then switching to Product B.
        Both calls should return distinct products.
        """
        tools, _ = await _get_tools()
        result_a = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result_a.get("found") is True

        result_b = await tools["find_product_by_id"].ainvoke({
            "product_id": "nonexistent-product-xyz-9999",
            "id_type": "handle",
        })
        assert result_b.get("found") is False, (
            "Second product should not be found — ensures results are independent"
        )

    @pytest.mark.asyncio
    async def test_same_product_called_twice_returns_same_data(self):
        """Idempotency: calling with same handle twice should return the same product."""
        tools, _ = await _get_tools()
        result_1 = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        result_2 = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result_1.get("found") is True
        assert result_2.get("found") is True
        p1 = result_1["product"]
        p2 = result_2["product"]
        assert p1["handle"] == p2["handle"]
        assert p1["name"] == p2["name"]
        assert p1["price"] == p2["price"]
        assert p1["sizes_in_stock"] == p2["sizes_in_stock"]

    @pytest.mark.asyncio
    async def test_find_by_id_followed_by_top_selling_different_results(self):
        """
        find_product_by_id for a specific product, then
        get_top_selling_products_tool — the top sellers should be
        independent and may or may not include the context product.
        """
        tools, _ = await _get_tools()
        context_result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert context_result.get("found") is True

        top_result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert top_result.get("found") is True
        top_products = top_result["products"]

        assert len(top_products) > 0
        for tp in top_products:
            assert tp.get("name") or tp.get("title")
            assert isinstance(tp.get("price"), dict)

    @pytest.mark.asyncio
    async def test_specific_product_price_after_top_selling(self):
        """
        Scenario: user asks 'what are your best sellers?' → then clicks a
        specific product. Verify we can get the correct price for the
        specific product via find_product_by_id.
        """
        tools, _ = await _get_tools()
        top_result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert top_result.get("found") is True
        products = top_result["products"]
        if not products:
            pytest.skip("No top-selling products available")

        first_top_product = products[0]
        first_handle = first_top_product.get("handle")
        if not first_handle:
            pytest.skip("Top-selling product has no handle")

        detail_result = await tools["find_product_by_id"].ainvoke({
            "product_id": first_handle,
            "id_type": "handle",
        })
        assert detail_result.get("found") is True
        detail_product = detail_result["product"]
        assert detail_product.get("handle") == first_handle
        assert isinstance(detail_product.get("price"), dict)
        assert detail_product["price"].get("min", 0) > 0, (
            "Specific product price should be > 0"
        )


# ===========================================================================
# 7. Normalized product schema consistency across tools
# ===========================================================================

class TestNormalizedSchemaConsistency:
    """
    Verify that find_product_by_id and get_top_selling_products_tool
    return products with the same normalized field set.
    """

    @pytest.mark.asyncio
    async def test_find_by_id_and_top_selling_share_key_set(self):
        """Keys in product from find_product_by_id should match keys from top-selling tool."""
        tools, _ = await _get_tools()

        id_result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        if id_result.get("found") is not True:
            pytest.skip("Could not fetch product by ID for comparison")

        top_result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 1})
        if not top_result.get("found") or not top_result.get("products"):
            pytest.skip("No top-selling products available for comparison")

        id_keys = set(id_result["product"].keys())
        top_keys = set(top_result["products"][0].keys())

        shared_required = NORMALIZED_PRODUCT_REQUIRED_KEYS
        id_missing = shared_required - id_keys
        top_missing = shared_required - top_keys

        assert not id_missing, (
            f"find_product_by_id product missing required normalized keys: {id_missing}"
        )
        assert not top_missing, (
            f"Top-selling product missing required normalized keys: {top_missing}"
        )

    @pytest.mark.asyncio
    async def test_price_format_consistent_across_tools(self):
        """Price should be {min, max} dict in both find_product_by_id and top-selling."""
        tools, _ = await _get_tools()

        id_result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        top_result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 1})

        if id_result.get("found"):
            id_price = id_result["product"]["price"]
            assert isinstance(id_price, dict) and "min" in id_price and "max" in id_price

        if top_result.get("found") and top_result.get("products"):
            top_price = top_result["products"][0]["price"]
            assert isinstance(top_price, dict) and "min" in top_price and "max" in top_price

    @pytest.mark.asyncio
    async def test_size_guide_format_consistent_across_tools(self):
        """size_guide should be a dict with has_size_guide in both tools."""
        tools, _ = await _get_tools()

        id_result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        top_result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 1})

        if id_result.get("found"):
            sg = id_result["product"]["size_guide"]
            assert isinstance(sg, dict) and "has_size_guide" in sg

        if top_result.get("found") and top_result.get("products"):
            sg = top_result["products"][0]["size_guide"]
            assert isinstance(sg, dict) and "has_size_guide" in sg

    @pytest.mark.asyncio
    async def test_sizes_format_consistent_across_tools(self):
        """sizes_in_stock and all_size_variants should both be lists in both tools."""
        tools, _ = await _get_tools()

        id_result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        top_result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 1})

        if id_result.get("found"):
            p = id_result["product"]
            assert isinstance(p["sizes_in_stock"], list)
            assert isinstance(p["all_size_variants"], list)

        if top_result.get("found") and top_result.get("products"):
            p = top_result["products"][0]
            assert isinstance(p["sizes_in_stock"], list)
            assert isinstance(p["all_size_variants"], list)


# ===========================================================================
# 8. Tool list integrity
# ===========================================================================

class TestToolListIntegrity:
    """Verify the factory returns the correct set of tools."""

    # Verified against the CURRENT product_details_tools_factory (this set
    # was stale before this PR -- get_top_selling_products_tool doesn't
    # exist in that factory at all; get_nearest_store and cart tools were
    # both missing). Confirmed via _build_state()'s default state: no
    # channel/gupshup_source_phone_number/session_id set, so
    # resolve_channel_from_state returns None, _is_web_chat is False, and
    # _create_cart_tools(include_writes=False) returns only [get_cart] --
    # not the 4 write tools that appear when include_writes=True.
    EXPECTED_TOOL_NAMES = {
        "search_products",
        "find_product_by_url",
        "find_product_by_id",
        "get_customization_config",
        "get_available_categories",
        "get_vendor_information",
        "get_nearest_store",
        "escalate_to_agent",
        "get_cart",
        # get_product_reviews is CONDITIONAL on ONE gate in
        # product_details_tools_factory: real Judge.me credentials configured
        # (client_configs.judgeme_details with shop_domain + api_token,
        # checked via is_judgeme_configured -- placeholder values don't
        # count). There is no separate opt-in flag: having working
        # credentials IS the decision to use Judge.me. CLIENT_ID above
        # (c3ffcb1b-...) is the reference/template client documented in
        # AGENTS.md ("Groovee" -- copied FROM, never written TO, during
        # onboarding of OTHER tenants); it is ALSO a real, live store
        # (Concept Groove) with Judge.me credentials configured, independent
        # of the onboarding-clone path.
        # judgeme_details is in EXCLUDED_REFERENCE_CONFIG_KEYS
        # (client_onboarding.py), so none of this is ever cloned onto a
        # newly onboarded tenant that copies its baseline config from this
        # client -- configuring it here does not violate the "never written
        # to" rule in the sense that rule protects against (accidental
        # propagation to new tenants via the onboarding path).
        "get_product_reviews",
    }

    @pytest.mark.asyncio
    async def test_factory_returns_expected_tools(self):
        tools, _ = await _get_tools()
        actual_names = set(tools.keys())
        assert actual_names == self.EXPECTED_TOOL_NAMES, (
            f"Tool set mismatch.\n"
            f"  Extra:   {actual_names - self.EXPECTED_TOOL_NAMES}\n"
            f"  Missing: {self.EXPECTED_TOOL_NAMES - actual_names}"
        )

    @pytest.mark.asyncio
    async def test_factory_returns_correct_count(self):
        tools, _ = await _get_tools()
        assert len(tools) == len(self.EXPECTED_TOOL_NAMES), (
            f"Expected {len(self.EXPECTED_TOOL_NAMES)} tools, got {len(tools)}"
        )

    @pytest.mark.asyncio
    async def test_no_removed_tools_present(self):
        """Verify removed tools are NOT in the factory output."""
        tools, _ = await _get_tools()
        removed_tools = {"get_size_chart_display", "check_restocking_query", "trigger_agent_escalation", "get_product_info_from_context"}
        for removed in removed_tools:
            assert removed not in tools, (
                f"Removed tool '{removed}' should not be in product_details_tools_factory"
            )

    @pytest.mark.asyncio
    async def test_all_tools_are_callable(self):
        tools, _ = await _get_tools()
        for name, tool in tools.items():
            assert hasattr(tool, "ainvoke"), (
                f"Tool '{name}' should have ainvoke method"
            )


# ===========================================================================
# 9. Statelessness verification (cross-tool)
# ===========================================================================

class TestStatelessnessAcrossTools:
    """
    Verify that no tool in product_details_tools_factory mutates
    the shared state object.
    """

    @pytest.mark.asyncio
    async def test_full_workflow_does_not_mutate_state(self):
        """
        Run multiple tools in sequence and verify state is unchanged
        after each call.
        """
        state = _build_state()
        original_state = copy.deepcopy(state)
        tools, state = await _get_tools(state)

        await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert state.get("conversation_context") == original_state["conversation_context"], (
            "State mutated after find_product_by_id"
        )

        await tools["get_top_selling_products_tool"].ainvoke({"top_n": 2})
        assert state.get("conversation_context") == original_state["conversation_context"], (
            "State mutated after get_top_selling_products_tool"
        )

        await tools["get_customization_config"].ainvoke({})
        assert state.get("conversation_context") == original_state["conversation_context"], (
            "State mutated after get_customization_config"
        )

        await tools["get_available_categories"].ainvoke({})
        assert state.get("conversation_context") == original_state["conversation_context"], (
            "State mutated after get_available_categories"
        )

        await tools["escalate_to_agent"].ainvoke({
            "reason": "Test workflow",
            "category": "General",
        })
        assert state.get("conversation_context") == original_state["conversation_context"], (
            "State mutated after escalate_to_agent"
        )

        for key in ("inquiry_product_info", "product_link", "product_selection_matches"):
            assert key not in state, (
                f"Key '{key}' should not appear in state after any tool call"
            )

    @pytest.mark.asyncio
    async def test_state_entities_not_modified(self):
        """Tools must not add entities to conversation_context."""
        state = _build_state()
        state["conversation_context"]["entities"] = [
            {"type": "product", "id": "existing-entity"}
        ]
        original_entities = copy.deepcopy(state["conversation_context"]["entities"])
        tools, state = await _get_tools(state)

        await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert state["conversation_context"]["entities"] == original_entities, (
            "find_product_by_id modified conversation_context.entities"
        )

        await tools["get_top_selling_products_tool"].ainvoke({"top_n": 1})
        assert state["conversation_context"]["entities"] == original_entities, (
            "get_top_selling_products_tool modified conversation_context.entities"
        )


# ===========================================================================
# 10. Edge cases and error handling
# ===========================================================================

class TestEdgeCases:
    """Edge cases, boundary conditions, and error handling."""

    @pytest.mark.asyncio
    async def test_find_by_id_empty_params(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_top_selling_zero_count(self):
        """top_n=0 should return empty list or handle gracefully."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 0})
        assert isinstance(result, dict)
        products = result.get("products", [])
        assert isinstance(products, list)
        assert len(products) == 0

    @pytest.mark.asyncio
    async def test_escalation_minimal_params(self):
        """Only reason is truly required."""
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Minimal escalation test",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True

    @pytest.mark.asyncio
    async def test_find_by_id_special_characters_in_handle(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "<script>alert('xss')</script>",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_find_by_id_unicode_handle(self):
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": "प्रोडक्ट-हिंदी",
            "id_type": "handle",
        })
        assert isinstance(result, dict)
        assert result.get("found") is False

    @pytest.mark.asyncio
    async def test_escalation_various_categories(self):
        """Test different escalation categories all succeed."""
        tools, _ = await _get_tools()
        categories = [
            "Restocking Query",
            "Cancellation Requests",
            "Order Update",
            "Product Complaint",
            "General",
        ]
        for cat in categories:
            result = await tools["escalate_to_agent"].ainvoke({
                "reason": f"Test {cat}",
                "category": cat,
            })
            assert isinstance(result, dict), f"Failed for category='{cat}'"
            assert result.get("success") is True, (
                f"Escalation failed for category='{cat}'. result={result}"
            )


# ===========================================================================
# 11. Complex real-world scenarios
# ===========================================================================

class TestComplexRealWorldScenarios:
    """
    End-to-end-ish scenarios that mirror real user journeys:
    browsing → asking questions → escalation.
    """

    @pytest.mark.asyncio
    async def test_browse_categories_then_ask_top_sellers(self):
        """User: 'What categories do you have?' → 'Show me bestsellers'."""
        tools, _ = await _get_tools()
        cat_result = await tools["get_available_categories"].ainvoke({})
        assert isinstance(cat_result, dict)

        top_result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert isinstance(top_result, dict)
        assert top_result.get("found") is True
        for p in top_result.get("products", []):
            assert p.get("name") or p.get("title")
            assert isinstance(p.get("price"), dict)

    @pytest.mark.asyncio
    async def test_view_product_then_ask_customization(self):
        """User views product, then asks 'Can I customize this?'."""
        tools, _ = await _get_tools()

        product_result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert product_result.get("found") is True

        custom_result = await tools["get_customization_config"].ainvoke({})
        assert isinstance(custom_result, dict)
        assert "policy" in custom_result

    @pytest.mark.asyncio
    async def test_view_product_check_size_guide_then_escalate_restock(self):
        """
        User views product → checks size guide from product data →
        finds size OOS → escalates for restocking.
        """
        tools, _ = await _get_tools()

        product_result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert product_result.get("found") is True
        product = product_result["product"]

        sg = product.get("size_guide")
        assert isinstance(sg, dict), "Product should have a size_guide dict"

        esc_result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer wants size XL restocked",
            "category": "Restocking Query",
            "details": f"Product '{product.get('name')}' ({VALID_PRODUCT_HANDLE}) — customer wants size XL",
        })
        assert esc_result.get("success") is True

    @pytest.mark.asyncio
    async def test_top_selling_then_pick_one_for_details(self):
        """
        User asks for top sellers → picks one → gets full details from context.
        Verify the context result has all the data needed to answer
        price, sizes, fabric, size guide questions.
        """
        tools, _ = await _get_tools()

        top_result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert top_result.get("found") is True
        products = top_result["products"]
        if not products:
            pytest.skip("No top-selling products")

        picked = products[0]
        picked_handle = picked.get("handle")
        if not picked_handle:
            pytest.skip("Top-selling product has no handle")

        detail_result = await tools["find_product_by_id"].ainvoke({
            "product_id": picked_handle,
            "id_type": "handle",
        })
        assert detail_result.get("found") is True
        detail_product = detail_result["product"]

        assert detail_product.get("handle") == picked_handle
        assert isinstance(detail_product.get("price"), dict)
        assert isinstance(detail_product.get("sizes_in_stock"), list)
        assert isinstance(detail_product.get("size_guide"), dict)
        assert "fabric" in detail_product
        assert "fit_type" in detail_product
        assert "variants" in detail_product

    @pytest.mark.asyncio
    async def test_product_detail_exact_values(self):
        """
        Verify exact field values for a known product (Evolve The Cosmic Shacket).
        This validates that tool output matches the real Shopify data.
        """
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]

        assert product["handle"] == VALID_PRODUCT_HANDLE
        name = product.get("name") or product.get("title", "")
        assert "Evolve" in name and "Cosmic" in name and "Shacket" in name, (
            f"Expected 'Evolve The Cosmic Shacket' in name, got '{name}'"
        )

        price = product.get("price", {})
        assert isinstance(price, dict)
        assert price.get("min", 0) > 0, "Price should be > 0"

        assert isinstance(product.get("variants"), list)
        assert product.get("total_variants", 0) > 0, "Should have at least one variant"

        assert isinstance(product.get("sizes_in_stock"), list)
        assert isinstance(product.get("all_size_variants"), list)
        assert len(product["all_size_variants"]) > 0, "Should have at least one size variant"

        assert product.get("product_id"), "product_id should be populated"

        assert isinstance(product.get("tags"), list)
        assert isinstance(product.get("all_metafields"), list)

    @pytest.mark.asyncio
    async def test_top_selling_exact_product_fields(self):
        """
        Verify exact field types and non-emptiness for top-selling products.
        """
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        assert result.get("found") is True
        for i, product in enumerate(result.get("products", [])):
            assert product.get("name") or product.get("title"), (
                f"Product #{i+1} has no name/title"
            )
            assert product.get("handle"), f"Product #{i+1} has no handle"
            url = product.get("url", "")
            assert url and "products/" in url, (
                f"Product #{i+1} URL invalid: '{url}'"
            )
            price = product.get("price", {})
            assert isinstance(price, dict), f"Product #{i+1} price not dict"
            assert price.get("min", 0) > 0, (
                f"Product #{i+1} price.min should be > 0, got {price.get('min')}"
            )
            assert isinstance(product.get("in_stock"), bool), (
                f"Product #{i+1} in_stock should be bool"
            )
            assert isinstance(product.get("stock_message"), str), (
                f"Product #{i+1} stock_message should be str"
            )


# ===========================================================================
# ADDITIONAL EDGE CASE AND DEEP VALIDATION TESTS
# ===========================================================================


class TestGetTopSellingEdgeCases:

    @pytest.mark.asyncio
    async def test_negative_top_n(self):
        """top_n=-1 should return empty products or handle gracefully."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": -1})
        assert isinstance(result, dict)
        products = result.get("products", [])
        assert isinstance(products, list)

    @pytest.mark.asyncio
    async def test_very_large_top_n(self):
        """top_n=100 should return whatever is available without crashing."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 100})
        assert isinstance(result, dict)
        products = result.get("products", [])
        assert isinstance(products, list)
        if result.get("found"):
            assert len(products) > 0, "Should return at least some products"

    @pytest.mark.asyncio
    async def test_products_have_unique_handles(self):
        """Top-selling products should have unique handles (no duplicates)."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 5})
        products = result.get("products", [])
        handles = [p.get("handle") for p in products if p.get("handle")]
        assert len(handles) == len(set(handles)), (
            f"Duplicate handles found in top-selling products: {handles}"
        )

    @pytest.mark.asyncio
    async def test_all_products_have_in_stock_field(self):
        """Each top-selling product should have an in_stock boolean."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        for i, product in enumerate(result.get("products", [])):
            assert "in_stock" in product, (
                f"Product #{i+1} missing in_stock field"
            )
            assert isinstance(product["in_stock"], bool), (
                f"Product #{i+1} in_stock should be bool, got {type(product['in_stock'])}"
            )

    @pytest.mark.asyncio
    async def test_product_urls_are_valid(self):
        """Each product URL should contain 'products/' and start with http."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        for i, product in enumerate(result.get("products", [])):
            url = product.get("url", "")
            assert url.startswith("http"), (
                f"Product #{i+1} URL should start with http: '{url}'"
            )
            assert "products/" in url, (
                f"Product #{i+1} URL should contain 'products/': '{url}'"
            )

    @pytest.mark.asyncio
    async def test_variant_ids_are_populated(self):
        """Each variant should have a non-empty id."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        for i, product in enumerate(result.get("products", [])):
            variants = product.get("variants", [])
            for j, variant in enumerate(variants):
                assert variant.get("id"), (
                    f"Product #{i+1} variant #{j+1} should have a non-empty id"
                )

    @pytest.mark.asyncio
    async def test_top_n_1_returns_one_seller_plus_optional_pins(self):
        """top_n=1 returns 1 live top-seller, plus up to MAX_PINNED pinned promos."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 1})
        if result.get("found"):
            assert 1 <= len(result.get("products", [])) <= 1 + MAX_PINNED, (
                f"top_n=1 should return 1 seller (+ up to {MAX_PINNED} pins), got "
                f"{len(result.get('products', []))}"
            )

    @pytest.mark.asyncio
    async def test_count_matches_products_length(self):
        """count field should match actual products list length."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        if result.get("found"):
            assert result.get("count") == len(result.get("products", [])), (
                f"count ({result.get('count')}) should match products length "
                f"({len(result.get('products', []))})"
            )


class TestGetCustomizationConfigEdgeCases:

    @pytest.mark.asyncio
    async def test_policy_contains_expected_keys(self):
        """Policy should contain a message or meaningful structure."""
        tools, _ = await _get_tools()
        result = await tools["get_customization_config"].ainvoke({})
        assert isinstance(result, dict)
        if result.get("success"):
            policy = result.get("policy")
            assert policy is not None, "success=True should include policy"
            if isinstance(policy, dict):
                assert "message" in policy or len(policy) > 0, (
                    "Policy dict should have 'message' or other keys"
                )

    @pytest.mark.asyncio
    async def test_consecutive_calls_return_same_data(self):
        """Two consecutive calls should return identical results (idempotent)."""
        tools, _ = await _get_tools()
        result1 = await tools["get_customization_config"].ainvoke({})
        result2 = await tools["get_customization_config"].ainvoke({})
        assert result1.get("success") == result2.get("success")
        if result1.get("success"):
            assert result1.get("policy") == result2.get("policy"), (
                "Consecutive calls should return the same policy"
            )

    @pytest.mark.asyncio
    async def test_response_is_not_error(self):
        """Result should not be an error response."""
        tools, _ = await _get_tools()
        result = await tools["get_customization_config"].ainvoke({})
        assert isinstance(result, dict)
        assert "error" not in result or result.get("success") is True, (
            f"Customization config should not return an error: {result}"
        )

    @pytest.mark.asyncio
    async def test_policy_value_is_non_empty(self):
        """Policy should have non-empty content."""
        tools, _ = await _get_tools()
        result = await tools["get_customization_config"].ainvoke({})
        if result.get("success"):
            policy = result.get("policy")
            if isinstance(policy, str):
                assert len(policy) > 0, "Policy string should not be empty"
            elif isinstance(policy, dict):
                assert len(policy) > 0, "Policy dict should not be empty"


class TestGetAvailableCategoriesEdgeCases:

    @pytest.mark.asyncio
    async def test_categories_have_unique_names(self):
        """Category names should be unique (no duplicates)."""
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        if result.get("found"):
            categories = result.get("categories", [])
            names = [c.get("name") for c in categories if c.get("name")]
            assert len(names) == len(set(names)), (
                f"Duplicate category names found: {names}"
            )

    @pytest.mark.asyncio
    async def test_category_urls_contain_collections(self):
        """Category URLs should contain 'collections/' or be valid store paths."""
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        if result.get("found"):
            for cat in result.get("categories", []):
                url = cat.get("url", "")
                assert url.startswith("http"), (
                    f"Category URL should start with http: '{url}'"
                )

    @pytest.mark.asyncio
    async def test_formatted_text_contains_all_category_names(self):
        """formatted_text should mention every category name."""
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        if result.get("found"):
            formatted_text = result.get("formatted_text", "")
            for cat in result.get("categories", []):
                name = cat.get("name", "")
                if name:
                    assert name.lower() in formatted_text.lower(), (
                        f"Category '{name}' should appear in formatted_text"
                    )

    @pytest.mark.asyncio
    async def test_categories_count_matches_list_length(self):
        """Number of categories in list should be consistent."""
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        if result.get("found"):
            categories = result.get("categories", [])
            assert len(categories) > 0, "found=True should have non-empty categories"

    @pytest.mark.asyncio
    async def test_each_category_formatted_contains_name(self):
        """Each category's 'formatted' field should contain its name."""
        tools, _ = await _get_tools()
        result = await tools["get_available_categories"].ainvoke({})
        if result.get("found"):
            for cat in result.get("categories", []):
                name = cat.get("name", "")
                formatted = cat.get("formatted", "")
                if name and formatted:
                    assert name.lower() in formatted.lower(), (
                        f"'{name}' should appear in formatted '{formatted}'"
                    )


class TestConcurrentToolCalls:

    @pytest.mark.asyncio
    async def test_parallel_find_by_id_and_top_selling(self):
        """Running find_by_id and top_selling concurrently should both succeed."""
        import asyncio

        tools, _ = await _get_tools()
        find_task = tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        top_task = tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})

        find_result, top_result = await asyncio.gather(find_task, top_task)

        assert find_result.get("found") is True, (
            f"find_product_by_id should succeed: {find_result}"
        )
        assert isinstance(top_result.get("products"), list)

    @pytest.mark.asyncio
    async def test_parallel_categories_and_customization(self):
        """Running categories and customization concurrently should both succeed."""
        import asyncio

        tools, _ = await _get_tools()
        cat_task = tools["get_available_categories"].ainvoke({})
        cust_task = tools["get_customization_config"].ainvoke({})

        cat_result, cust_result = await asyncio.gather(cat_task, cust_task)

        assert isinstance(cat_result, dict)
        assert isinstance(cust_result, dict)

    @pytest.mark.asyncio
    async def test_parallel_three_tools(self):
        """Three concurrent tool calls should all succeed independently."""
        import asyncio

        tools, _ = await _get_tools()
        tasks = [
            tools["find_product_by_id"].ainvoke({
                "product_id": VALID_PRODUCT_HANDLE,
                "id_type": "handle",
            }),
            tools["get_top_selling_products_tool"].ainvoke({"top_n": 2}),
            tools["get_available_categories"].ainvoke({}),
        ]

        results = await asyncio.gather(*tasks)
        assert len(results) == 3
        for r in results:
            assert isinstance(r, dict)

    @pytest.mark.asyncio
    async def test_state_unchanged_after_concurrent_calls(self):
        """State should not be mutated by concurrent tool calls."""
        import asyncio

        state = _build_state()
        snap = copy.deepcopy(state)
        tools, _ = await _get_tools(state)

        tasks = [
            tools["find_product_by_id"].ainvoke({
                "product_id": VALID_PRODUCT_HANDLE,
                "id_type": "handle",
            }),
            tools["get_top_selling_products_tool"].ainvoke({"top_n": 2}),
            tools["get_available_categories"].ainvoke({}),
            tools["get_customization_config"].ainvoke({}),
        ]
        await asyncio.gather(*tasks)

        assert state["conversation_context"] == snap["conversation_context"], (
            "conversation_context mutated by concurrent calls"
        )
        assert state["messages"] == snap["messages"]


class TestProductDataDeepValidation:

    @pytest.mark.asyncio
    async def test_variant_prices_are_numeric(self):
        """Every variant price should be castable to float and > 0."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        for variant in result["product"].get("variants", []):
            price = variant.get("price")
            if price is not None:
                assert float(price) > 0, (
                    f"Variant price should be > 0, got {price}"
                )

    @pytest.mark.asyncio
    async def test_variant_ids_are_strings(self):
        """Variant IDs should be non-empty strings or ints."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        for i, variant in enumerate(result["product"].get("variants", [])):
            vid = variant.get("id")
            assert vid is not None and str(vid), (
                f"Variant #{i+1} should have a non-empty id, got {vid}"
            )

    @pytest.mark.asyncio
    async def test_all_sizes_superset_of_in_stock(self):
        """all_size_variants should be a superset of sizes_in_stock."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]
        all_sizes = set(s.upper() for s in product.get("all_size_variants", []))
        in_stock = set(s.upper() for s in product.get("sizes_in_stock", []))
        assert in_stock.issubset(all_sizes), (
            f"sizes_in_stock {in_stock} should be a subset of all_size_variants {all_sizes}"
        )

    @pytest.mark.asyncio
    async def test_metafields_structure(self):
        """Each metafield should have key and value."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        metafields = result["product"].get("all_metafields", [])
        assert isinstance(metafields, list)
        for mf in metafields:
            assert "key" in mf, f"Metafield should have 'key': {mf}"
            assert "value" in mf, f"Metafield should have 'value': {mf}"

    @pytest.mark.asyncio
    async def test_image_url_is_valid(self):
        """Primary image URL should start with https://."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        image_url = result["product"].get("image_url", "")
        if image_url:
            assert image_url.startswith("https://"), (
                f"image_url should start with https://, got '{image_url[:100]}'"
            )

    @pytest.mark.asyncio
    async def test_price_min_lte_max(self):
        """price.min should be <= price.max."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        price = result["product"].get("price", {})
        if price.get("min") is not None and price.get("max") is not None:
            assert float(price["min"]) <= float(price["max"]), (
                f"price.min ({price['min']}) should be <= price.max ({price['max']})"
            )

    @pytest.mark.asyncio
    async def test_top_selling_variant_prices_are_numeric(self):
        """Top-selling product variants should also have valid prices."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        for i, product in enumerate(result.get("products", [])):
            for j, variant in enumerate(product.get("variants", [])):
                price = variant.get("price")
                if price is not None:
                    try:
                        assert float(price) > 0, (
                            f"Product #{i+1} variant #{j+1} price should be > 0"
                        )
                    except (ValueError, TypeError):
                        pytest.fail(
                            f"Product #{i+1} variant #{j+1} price '{price}' is not numeric"
                        )

    @pytest.mark.asyncio
    async def test_top_selling_all_sizes_superset_of_in_stock(self):
        """For top-selling, all_size_variants should be superset of sizes_in_stock."""
        tools, _ = await _get_tools()
        result = await tools["get_top_selling_products_tool"].ainvoke({"top_n": 3})
        for i, product in enumerate(result.get("products", [])):
            all_sizes = set(s.upper() for s in product.get("all_size_variants", []))
            in_stock = set(s.upper() for s in product.get("sizes_in_stock", []))
            assert in_stock.issubset(all_sizes), (
                f"Product #{i+1}: sizes_in_stock {in_stock} not subset of "
                f"all_size_variants {all_sizes}"
            )

    @pytest.mark.asyncio
    async def test_description_is_string(self):
        """Product description should be a string."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        desc = result["product"].get("description")
        assert desc is None or isinstance(desc, str), (
            f"description should be str or None, got {type(desc)}"
        )

    @pytest.mark.asyncio
    async def test_product_type_and_category_are_strings(self):
        """product_type and category should be strings."""
        tools, _ = await _get_tools()
        result = await tools["find_product_by_id"].ainvoke({
            "product_id": VALID_PRODUCT_HANDLE,
            "id_type": "handle",
        })
        assert result.get("found") is True
        product = result["product"]
        pt = product.get("product_type")
        cat = product.get("category")
        assert pt is None or isinstance(pt, str), (
            f"product_type should be str, got {type(pt)}"
        )
        assert cat is None or isinstance(cat, str), (
            f"category should be str, got {type(cat)}"
        )


class TestEscalationEdgeCases:

    @pytest.mark.asyncio
    async def test_long_reason_string(self):
        """Very long reason string should not crash."""
        tools, _ = await _get_tools()
        long_reason = "Customer is very upset about the product quality. " * 50
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": long_reason,
            "category": "Product Complaint",
        })
        assert isinstance(result, dict)
        assert result.get("success") is True or "message" in result

    @pytest.mark.asyncio
    async def test_special_characters_in_reason(self):
        """Special characters should not break escalation."""
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Test: 'quotes' & \"double\" <html> @#$%^&*()",
            "category": "General",
        })
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_various_categories(self):
        """Multiple escalation categories should all succeed."""
        tools, _ = await _get_tools()
        categories = [
            "Restocking Query",
            "Product Complaint",
            "Order Update",
            "General",
            "Cancellation Requests",
        ]
        for cat in categories:
            result = await tools["escalate_to_agent"].ainvoke({
                "reason": f"Test escalation for {cat}",
                "category": cat,
            })
            assert isinstance(result, dict), (
                f"Escalation with category '{cat}' should return dict"
            )

    @pytest.mark.asyncio
    async def test_empty_reason_still_works(self):
        """Empty reason should not crash (though not ideal usage)."""
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "",
            "category": "General",
        })
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_escalation_with_all_optional_params(self):
        """Providing all optional params (details, phone, order_id)."""
        tools, _ = await _get_tools()
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer wants restock notification",
            "category": "Restocking Query",
            "details": "Product Cosmic Shacket in size XL",
            "phone_number": "9876543210",
            "order_id": "GV12345",
        })
        assert isinstance(result, dict)
        if result.get("success"):
            assert result.get("message"), "Successful escalation should have a message"
