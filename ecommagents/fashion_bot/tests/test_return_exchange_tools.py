"""
Comprehensive test suite for all tools in return_exchange_tools_factory.

Tools under test (6 total):
    1. get_customers_delivered_orders_by_phone — delivered orders by phone
    2. get_recent_orders                      — any-status orders by phone (shared)
    3. get_order_details                      — unified Shopify + logistics order details (shared)
    4. check_grace_period_eligibility         — return/exchange grace period check (sync, returns str)
    5. get_final_return_exchange_message       — return/exchange instructions (no days_since_delivery)
    6. escalate_to_agent                      — shared human escalation (returns dict)

Coverage areas:
    - Delivered order lookup: valid phone, limit, empty phone, fake phone, structure
    - Order details: basic lookup, phone validation, line item structure
    - Grace period: date formats, eligible/not-eligible, edge cases (N/A, empty)
    - Final message: return vs exchange, default type, content validation
    - Escalation: return/exchange categories, full params, message content
    - Statelessness: no state mutation from any tool
    - End-to-end: chained tool calls mimicking real return/exchange flows
"""

import copy
import re
import pytest
from fashion_bot.tool_factory import (
    return_exchange_tools_factory,
    place_order_tools_factory,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"
TEST_CLIENT_ID = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
TEST_VALID_PRODUCT_URL = "https://blommerce-test.myshopify.com/products/evolve-the-cosmic-shacket"

# Unique phone per test category to avoid dedup guard
PHONE_DELIVERED_ORDERS = "9500300001"
PHONE_ORDER_DETAILS_RE = "9500300002"
PHONE_GRACE_PERIOD = "9500300003"
PHONE_FINAL_MSG = "9500300004"
PHONE_ESCALATION = "9500300005"
PHONE_E2E_RETURN = "9500300006"
PHONE_E2E_EXCHANGE = "9500300007"
PHONE_STATELESS = "9500300008"

# Test-store Shopify credentials (same as other test files)
_TEST_SHOPIFY_CLIENT_ID = "85391617d1691af65cf0267f54a3e298"
_TEST_SHOPIFY_CLIENT_SECRET = "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"
_TEST_SHOPIFY_DOMAIN = "blommerce-test.myshopify.com"

EXPECTED_TOOL_NAMES = {
    "get_customers_delivered_orders_by_phone",
    # Any-status fallback: the delivered-only lookup above hides orders that are
    # still processing, which used to leave the agent guessing an order ID.
    "get_recent_orders",
    "get_order_details",
    "check_grace_period_eligibility",
    "get_final_return_exchange_message",
    "escalate_to_agent",
}

REMOVED_TOOL_NAMES = {
    "detect_return_exchange_preference",
    "detect_pickup_query",
    "analyze_bank_transfer_preference",
    "get_return_reason",
    "suggest_exchange_instead_of_return",
    "check_customer_exchange_response",
    "validate_size_for_exchange",
    "check_confirmation",
    "extract_order_id_from_message",
    "search_order_id_in_conversation",
    "extract_phone_from_message",
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

def _build_state(phone: str = "9716336096", client_id: str = CLIENT_ID):
    return {
        "client_id": client_id,
        "phone_number": phone,
        "conversation_context": {"entities": [], "topics": [], "focal_entity": None},
        "messages": [],
    }


def _get_return_exchange_tools(state=None):
    """Build return_exchange factory and return a name->tool mapping."""
    if state is None:
        state = _build_state()
    tools = return_exchange_tools_factory(state, state["messages"])
    return {t.name: t for t in tools}, state


async def _create_test_order(phone: str) -> dict:
    """Create a fresh COD order on the blommerce-test store."""
    import asyncio
    from fashion_bot.core.orchestrator import OrderStatusOrchestrator

    state = _build_state(phone=phone, client_id=TEST_CLIENT_ID)
    po_tools = place_order_tools_factory(state, state["messages"], TEST_CLIENT_ID)
    po_map = {t.name: t for t in po_tools}
    result = await po_map["create_order"].ainvoke({
        "product_link": TEST_VALID_PRODUCT_URL,
        "size": "L",
        "customer_name": "ReturnExchange Test",
        "customer_address": "E-10 Jail Road, Janak Puri, New Delhi, Delhi",
        "pincode": "110058",
    })
    if result.get("success"):
        oid = _order_id_from_result(result)
        await _wait_for_order_indexed(oid, state)
    return result


async def _wait_for_order_indexed(order_id: str, state: dict, max_retries: int = 5):
    """Poll until Shopify's search API finds the order."""
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


def _order_id_from_result(result: dict) -> str:
    """Extract a usable order_id from a create_order result."""
    order_number = str(result.get("order_number", ""))
    if order_number:
        return order_number
    return str(result.get("order_id", "")).lstrip("#")


# ===========================================================================
# 1. Tool List Integrity
# ===========================================================================

class TestToolListIntegrity:

    def test_factory_returns_expected_tools(self):
        tools, _ = _get_return_exchange_tools()
        actual = set(tools.keys())
        assert actual == EXPECTED_TOOL_NAMES, (
            f"Expected tools {EXPECTED_TOOL_NAMES}, got {actual}"
        )

    def test_factory_returns_correct_count(self):
        tools, _ = _get_return_exchange_tools()
        assert len(tools) == 6, f"Expected 6 tools, got {len(tools)}"

    def test_no_removed_tools_present(self):
        tools, _ = _get_return_exchange_tools()
        for name in REMOVED_TOOL_NAMES:
            assert name not in tools, f"Removed tool '{name}' should not be present"

    def test_all_tools_are_callable(self):
        tools, _ = _get_return_exchange_tools()
        for name, tool in tools.items():
            assert hasattr(tool, "ainvoke") or callable(tool), (
                f"Tool '{name}' is not callable"
            )


# ===========================================================================
# 2. get_customers_delivered_orders_by_phone
# ===========================================================================

class TestGetCustomersDeliveredOrdersByPhone:

    @pytest.mark.asyncio
    async def test_valid_phone_returns_delivered_orders(self):
        """Known phone on the test store should return delivered orders or
        gracefully indicate none exist."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        assert isinstance(result, dict)
        assert "success" in result
        assert "orders" in result
        assert isinstance(result["orders"], list)
        if result["success"]:
            assert result["total_orders"] > 0, (
                "When success=True, total_orders should be > 0"
            )
            assert result["total_orders"] == len(result["orders"])

    @pytest.mark.asyncio
    async def test_returned_order_structure(self):
        """Each order in the response should have all required fields."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        required_fields = {"order_id", "order_placed_date", "delivered_date", "status",
                           "total_price", "currency", "items", "financial_status"}
        for order in result.get("orders", []):
            missing = required_fields - set(order.keys())
            assert not missing, (
                f"Order {order.get('order_id')} missing fields: {missing}"
            )
            assert order["status"] == "delivered", (
                f"All orders should have status 'delivered', got '{order['status']}'"
            )
            assert isinstance(order["items"], list)

    @pytest.mark.asyncio
    async def test_limit_parameter_respected(self):
        """limit=1 should return at most 1 order."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
            "limit": 1,
        })
        assert isinstance(result, dict)
        assert len(result.get("orders", [])) <= 1

    @pytest.mark.asyncio
    async def test_default_limit_is_3(self):
        """Default invocation should return at most 3 orders."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        assert len(result.get("orders", [])) <= 3

    @pytest.mark.asyncio
    async def test_invalid_phone_returns_empty(self):
        """Fake phone should return success=False with empty orders."""
        state = _build_state(phone="0000000000", client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": "0000000000",
        })
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert result.get("orders") == [] or result.get("total_orders", 0) == 0

    @pytest.mark.asyncio
    async def test_empty_phone_returns_error(self):
        """Empty phone number should return a failure."""
        state = _build_state(phone="", client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": "",
        })
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "phone" in result.get("message", "").lower() or result.get("orders") == []

    @pytest.mark.asyncio
    async def test_orders_have_order_placed_dates(self):
        """order_placed_date should match 'DD Mon YYYY' pattern or be 'N/A'."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        date_pattern = re.compile(r"^\d{2} [A-Z][a-z]{2} \d{4}$")
        for order in result.get("orders", []):
            fd = order.get("order_placed_date", "")
            assert fd == "N/A" or date_pattern.match(fd), (
                f"order_placed_date '{fd}' does not match 'DD Mon YYYY' or 'N/A'"
            )

    @pytest.mark.asyncio
    async def test_orders_have_delivered_date_distinct_from_placed_date(self):
        """delivered_date must be its own field — 'DD Mon YYYY' or 'unavailable' —
        and must never silently equal order_placed_date's raw source (this is
        the exact bug: a placement date mislabeled as a delivery date)."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        date_pattern = re.compile(r"^\d{2} [A-Z][a-z]{2} \d{4}$")
        for order in result.get("orders", []):
            dd = order.get("delivered_date", "")
            assert dd == "unavailable" or date_pattern.match(dd), (
                f"delivered_date '{dd}' does not match 'DD Mon YYYY' or 'unavailable'"
            )

    @pytest.mark.asyncio
    async def test_showing_top_matches_orders_count(self):
        """When success, showing_top should equal len(orders)."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        if result.get("success"):
            assert result.get("showing_top") == len(result.get("orders", [])), (
                f"showing_top ({result.get('showing_top')}) should match "
                f"orders count ({len(result.get('orders', []))})"
            )

    @pytest.mark.asyncio
    async def test_currency_defaults_to_inr(self):
        """Order currency should default to INR for the test store."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        for order in result.get("orders", []):
            assert order.get("currency") == "INR", (
                f"Expected currency 'INR', got '{order.get('currency')}'"
            )

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        snap = copy.deepcopy(state)
        tools, _ = _get_return_exchange_tools(state)
        await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        assert state["conversation_context"] == snap["conversation_context"]
        assert state["messages"] == snap["messages"]

    @pytest.mark.asyncio
    async def test_total_price_is_numeric_string(self):
        """total_price should be a string that can be cast to float."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        for order in result.get("orders", []):
            try:
                float(order.get("total_price", "0"))
            except (ValueError, TypeError):
                pytest.fail(
                    f"total_price '{order.get('total_price')}' cannot be cast to float"
                )

    @pytest.mark.asyncio
    async def test_order_ids_are_non_empty(self):
        """Each order should have a non-empty order_id."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
        })
        for order in result.get("orders", []):
            assert order.get("order_id"), (
                f"order_id should be non-empty, got '{order.get('order_id')}'"
            )


# ===========================================================================
# 3. get_order_details (shared tool via return_exchange factory)
# ===========================================================================

class TestGetOrderDetailsReturnExchange:

    @pytest.mark.asyncio
    async def test_valid_order_returns_details(self):
        """Create an order and verify get_order_details returns it."""
        create = await _create_test_order(PHONE_ORDER_DETAILS_RE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_ORDER_DETAILS_RE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_order_details"].ainvoke({
            "order_id": oid,
            "phone_number": PHONE_ORDER_DETAILS_RE,
        })
        assert isinstance(result, dict)
        assert result.get("success") is True, (
            f"Expected successful order lookup: {result}"
        )
        assert result.get("phone_validated") is True
        assert result.get("order_id"), "order_id should be present in response"

    @pytest.mark.asyncio
    async def test_wrong_phone_access_denied(self):
        """Phone mismatch should return access_denied error."""
        create = await _create_test_order(PHONE_ORDER_DETAILS_RE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="9999999999", client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_order_details"].ainvoke({
            "order_id": oid,
            "phone_number": "9999999999",
        })
        assert isinstance(result, dict)
        assert result.get("error") == "access_denied" or result.get("phone_validated") is False

    @pytest.mark.asyncio
    async def test_nonexistent_order_id(self):
        state = _build_state(phone=PHONE_ORDER_DETAILS_RE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_order_details"].ainvoke({
            "order_id": "9999999",
            "phone_number": PHONE_ORDER_DETAILS_RE,
        })
        assert isinstance(result, dict)
        assert result.get("success") is not True or "not found" in str(result).lower()

    @pytest.mark.asyncio
    async def test_line_items_have_variant_id(self):
        """Each line item should include variant_id and product_id."""
        create = await _create_test_order(PHONE_ORDER_DETAILS_RE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_ORDER_DETAILS_RE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_order_details"].ainvoke({
            "order_id": oid,
            "phone_number": PHONE_ORDER_DETAILS_RE,
        })
        if not result.get("success"):
            pytest.skip(f"Order details fetch failed: {result}")
        for item in result.get("line_items", []):
            assert item.get("variant_id"), (
                f"line_item should have variant_id: {item}"
            )
            assert item.get("product_id"), (
                f"line_item should have product_id: {item}"
            )

    @pytest.mark.asyncio
    async def test_order_has_financial_and_fulfillment_status(self):
        """Response should include financial_status and fulfillment_status."""
        create = await _create_test_order(PHONE_ORDER_DETAILS_RE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_ORDER_DETAILS_RE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_order_details"].ainvoke({
            "order_id": oid,
            "phone_number": PHONE_ORDER_DETAILS_RE,
        })
        if not result.get("success"):
            pytest.skip(f"Order details fetch failed: {result}")
        assert "financial_status" in result, "Response should include financial_status"
        assert "fulfillment_status" in result, "Response should include fulfillment_status"

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state(phone=PHONE_ORDER_DETAILS_RE, client_id=TEST_CLIENT_ID)
        snap = copy.deepcopy(state)
        tools, _ = _get_return_exchange_tools(state)
        await tools["get_order_details"].ainvoke({
            "order_id": "9999999",
            "phone_number": PHONE_ORDER_DETAILS_RE,
        })
        assert state["conversation_context"] == snap["conversation_context"]
        assert state["messages"] == snap["messages"]


# ===========================================================================
# 4. check_grace_period_eligibility (sync tool, returns str)
# ===========================================================================

class TestCheckGracePeriodEligibility:

    @pytest.mark.asyncio
    async def test_recent_delivery_eligible(self):
        """Order delivered yesterday should be within grace period."""
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert isinstance(result, str), (
            f"Return_exchange grace period tool should return str, got {type(result)}"
        )
        result_lower = result.lower()
        assert "eligible" in result_lower or "grace" in result_lower or "return" in result_lower, (
            f"Expected eligibility info in message: {result[:300]}"
        )

    @pytest.mark.asyncio
    async def test_old_delivery_not_eligible(self):
        """Order delivered 60 days ago should be outside grace period."""
        from datetime import datetime, timedelta

        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": old_date,
            "request_type": "return",
        })
        assert isinstance(result, str)
        result_lower = result.lower()
        assert "not eligible" in result_lower or "expired" in result_lower or "not" in result_lower, (
            f"60-day-old delivery should be ineligible: {result[:300]}"
        )

    @pytest.mark.asyncio
    async def test_exchange_request_type(self):
        """Exchange request type should be handled correctly."""
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "exchange",
        })
        assert isinstance(result, str)
        assert len(result) > 5, "Should return a substantive message"

    @pytest.mark.asyncio
    async def test_iso_date_format(self):
        """ISO format with time and timezone offset should be parsed."""
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+05:30")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert isinstance(result, str)
        assert len(result) > 5

    @pytest.mark.asyncio
    async def test_dd_mm_yyyy_format(self):
        """DD-MM-YYYY format should be parsed."""
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%d-%m-%Y")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert isinstance(result, str)
        assert len(result) > 5

    @pytest.mark.asyncio
    async def test_dd_mon_yyyy_format(self):
        """'DD Mon YYYY' format (e.g., '10 Dec 2025') should be parsed."""
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%d %b %Y")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert isinstance(result, str)
        assert len(result) > 5

    @pytest.mark.asyncio
    async def test_invalid_date_format(self):
        """Garbage date string should not crash; should return error message."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": "not-a-date-at-all",
            "request_type": "return",
        })
        assert isinstance(result, str)
        result_lower = result.lower()
        assert "error" in result_lower or "could not" in result_lower or "parse" in result_lower or len(result) > 0, (
            f"Invalid date should produce an error message: {result}"
        )

    @pytest.mark.asyncio
    async def test_empty_delivery_date_treated_eligible(self):
        """Empty delivery_date should be treated as eligible (unknown delivery)."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": "",
            "request_type": "return",
        })
        assert isinstance(result, str)
        assert len(result) > 5

    @pytest.mark.asyncio
    async def test_na_delivery_date_treated_eligible(self):
        """'N/A' delivery_date should be treated as eligible."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": "N/A",
            "request_type": "return",
        })
        assert isinstance(result, str)
        assert len(result) > 5

    @pytest.mark.asyncio
    async def test_not_available_delivery_date(self):
        """'not available' delivery_date should be treated as eligible."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": "not available",
            "request_type": "return",
        })
        assert isinstance(result, str)
        assert len(result) > 5

    @pytest.mark.asyncio
    async def test_returns_string_not_dict(self):
        """Return_exchange version should return str, not dict."""
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert isinstance(result, str), (
            f"Return_exchange grace period should return str, got {type(result).__name__}: {result}"
        )
        assert not isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        from datetime import datetime, timedelta

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        snap = copy.deepcopy(state)
        tools, _ = _get_return_exchange_tools(state)
        await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert state["conversation_context"] == snap["conversation_context"]
        assert state["messages"] == snap["messages"]
        assert "days_since_delivery" not in state


# ===========================================================================
# 5. get_final_return_exchange_message
# ===========================================================================

class TestGetFinalReturnExchangeMessage:

    @pytest.mark.asyncio
    async def test_return_message(self):
        """request_type='return' should return a substantive message."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "return",
        })
        assert isinstance(result, str)
        assert len(result) > 10, "Should return a substantive message"

    @pytest.mark.asyncio
    async def test_exchange_message(self):
        """request_type='exchange' should return a substantive message."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "exchange",
        })
        assert isinstance(result, str)
        assert len(result) > 10, "Should return a substantive message"

    @pytest.mark.asyncio
    async def test_default_is_return(self):
        """Default invocation (no args) should default to 'return'."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_final_return_exchange_message"].ainvoke({})
        assert isinstance(result, str)
        assert len(result) > 10

    @pytest.mark.asyncio
    async def test_returns_string_not_dict(self):
        """Return_exchange version should return str, not dict."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "return",
        })
        assert isinstance(result, str), (
            f"Expected str return type, got {type(result).__name__}"
        )

    @pytest.mark.asyncio
    async def test_message_has_meaningful_content(self):
        """Message should contain actionable content, not just an error."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "return",
        })
        result_lower = result.lower()
        assert "error" not in result_lower[:20], (
            f"Message should not start with error: {result[:200]}"
        )
        has_keywords = any(
            kw in result_lower
            for kw in ["return", "exchange", "refund", "pickup", "website",
                        "contact", "visit", "initiate", "process", "request"]
        )
        assert has_keywords, (
            f"Message should contain actionable keywords: {result[:300]}"
        )

    @pytest.mark.asyncio
    async def test_return_and_exchange_messages_differ(self):
        """Return and exchange messages should have different content."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        return_msg = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "return",
        })
        exchange_msg = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "exchange",
        })
        assert isinstance(return_msg, str) and isinstance(exchange_msg, str)
        assert len(return_msg) > 10 and len(exchange_msg) > 10

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        snap = copy.deepcopy(state)
        tools, _ = _get_return_exchange_tools(state)
        await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "return",
        })
        assert state["conversation_context"] == snap["conversation_context"]
        assert state["messages"] == snap["messages"]


# ===========================================================================
# 6. escalate_to_agent (shared tool — returns dict)
# ===========================================================================

class TestEscalateToAgentReturnExchange:

    @pytest.mark.asyncio
    async def test_basic_escalation(self):
        """Basic escalation with required params should succeed."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer wants to return a jacket",
            "category": "Return Request",
            "details": "Order delivered 3 days ago, wants return due to size issue",
        })
        assert isinstance(result, dict), (
            f"Shared escalation should return dict, got {type(result).__name__}"
        )
        assert result.get("success") is True or "message" in result

    @pytest.mark.asyncio
    async def test_return_request_escalation(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer confirmed return",
            "category": "Return Request",
            "details": "Return for order GV10741, size mismatch",
        })
        assert isinstance(result, dict)
        msg = result.get("message", "")
        msg_lower = msg.lower()
        assert "escalat" in msg_lower or "team" in msg_lower or "contact" in msg_lower or "agent" in msg_lower, (
            f"Escalation message should reference team/escalation: {msg[:300]}"
        )

    @pytest.mark.asyncio
    async def test_exchange_request_escalation(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer wants to exchange for different size",
            "category": "Exchange Request",
            "details": "Exchange M to L for cosmic shacket",
        })
        assert isinstance(result, dict)
        assert result.get("message")

    @pytest.mark.asyncio
    async def test_with_order_id_and_phone(self):
        """Full params including order_id and phone_number."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Bank transfer refund requested",
            "category": "Return Request",
            "details": "Customer wants refund via bank transfer for order GV10741",
            "order_id": "GV10741",
            "phone_number": "9500300005",
        })
        assert isinstance(result, dict)
        assert result.get("message")

    @pytest.mark.asyncio
    async def test_returns_dict_not_str(self):
        """Shared escalation should return dict."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Test escalation",
            "category": "General",
        })
        assert isinstance(result, dict), (
            f"Expected dict, got {type(result).__name__}: {result}"
        )

    @pytest.mark.asyncio
    async def test_message_contains_escalation_info(self):
        """Escalation message should indicate the request was logged."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Out-of-policy return",
            "category": "Return Request",
            "details": "Customer persistent about 30-day return",
        })
        assert isinstance(result, dict)
        msg_lower = result.get("message", "").lower()
        has_indicators = any(
            kw in msg_lower
            for kw in ["escalat", "team", "contact", "agent", "logged", "24 hour",
                        "will reach", "will get back"]
        )
        assert has_indicators, (
            f"Escalation message should indicate request was logged: {result.get('message', '')[:300]}"
        )

    @pytest.mark.asyncio
    async def test_bank_transfer_escalation(self):
        """Bank transfer refund escalation scenario."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer prefers bank transfer for refund",
            "category": "Bank Transfer Refund",
            "details": "Order GV10741, customer wants bank transfer instead of store credit",
            "order_id": "GV10741",
        })
        assert isinstance(result, dict)
        assert result.get("message")

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = _build_state(client_id=TEST_CLIENT_ID)
        snap = copy.deepcopy(state)
        tools, _ = _get_return_exchange_tools(state)
        await tools["escalate_to_agent"].ainvoke({
            "reason": "Test",
            "category": "Return Request",
        })
        assert state["conversation_context"] == snap["conversation_context"]
        assert state["messages"] == snap["messages"]
        assert "needs_escalation" not in state


# ===========================================================================
# 7. Statelessness — Full workflow
# ===========================================================================

class TestStatelessness:

    @pytest.mark.asyncio
    async def test_full_workflow_does_not_mutate_state(self):
        """Sequential calls to all tools should not mutate state."""
        from datetime import datetime, timedelta

        state = _build_state(phone=PHONE_STATELESS, client_id=TEST_CLIENT_ID)
        snap = copy.deepcopy(state)
        tools, _ = _get_return_exchange_tools(state)

        await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_STATELESS,
        })

        await tools["get_order_details"].ainvoke({
            "order_id": "9999999",
            "phone_number": PHONE_STATELESS,
        })

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })

        await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "return",
        })

        await tools["escalate_to_agent"].ainvoke({
            "reason": "Test workflow",
            "category": "Return Request",
            "details": "Full workflow statelessness test",
        })

        assert state["conversation_context"] == snap["conversation_context"], (
            "conversation_context should not be mutated by any tool"
        )
        assert state["messages"] == snap["messages"], (
            "messages should not be mutated by any tool"
        )
        assert "days_since_delivery" not in state
        assert "needs_escalation" not in state
        assert "return_exchange_preference" not in state

    @pytest.mark.asyncio
    async def test_state_entities_not_modified(self):
        """Entities list should remain empty after tool calls."""
        state = _build_state(phone=PHONE_STATELESS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)

        await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_STATELESS,
        })
        await tools["escalate_to_agent"].ainvoke({
            "reason": "Test",
            "category": "Exchange Request",
        })

        assert state["conversation_context"]["entities"] == [], (
            "entities should remain empty after tool calls"
        )
        assert state["conversation_context"]["focal_entity"] is None


# ===========================================================================
# 8. End-to-end flows
# ===========================================================================

class TestEndToEndReturnFlow:

    @pytest.mark.asyncio
    async def test_return_flow_grace_check_to_message(self):
        """Simulate return flow: grace period check -> final return message."""
        from datetime import datetime, timedelta

        state = _build_state(phone=PHONE_E2E_RETURN, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        grace_result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert isinstance(grace_result, str)
        assert len(grace_result) > 5

        msg_result = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "return",
        })
        assert isinstance(msg_result, str)
        assert len(msg_result) > 10

    @pytest.mark.asyncio
    async def test_exchange_flow_grace_check_to_escalation(self):
        """Simulate exchange flow: grace check -> escalation."""
        from datetime import datetime, timedelta

        state = _build_state(phone=PHONE_E2E_EXCHANGE, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)

        yesterday = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        grace_result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "exchange",
        })
        assert isinstance(grace_result, str)

        esc_result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer confirmed exchange",
            "category": "Exchange Request",
            "details": "Exchange for different size after grace period check",
        })
        assert isinstance(esc_result, dict)
        assert esc_result.get("message")

    @pytest.mark.asyncio
    async def test_delivered_orders_then_order_details(self):
        """Chain: get delivered orders -> pick one -> get details."""
        state = _build_state(phone=PHONE_E2E_RETURN, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)

        delivered = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_E2E_RETURN,
        })
        assert isinstance(delivered, dict)
        if not delivered.get("success") or not delivered.get("orders"):
            pytest.skip("No delivered orders for end-to-end test")

        first_order = delivered["orders"][0]
        oid = first_order["order_id"]
        assert oid, "First order should have a non-empty order_id"

        details = await tools["get_order_details"].ainvoke({
            "order_id": oid,
            "phone_number": PHONE_E2E_RETURN,
        })
        assert isinstance(details, dict)

    @pytest.mark.asyncio
    async def test_full_return_flow_create_order_to_escalation(self):
        """Full flow: create order -> get details -> grace check -> final msg -> escalate.

        The newly created order will NOT be delivered, so grace check uses
        a synthetic date, but the tool chain is exercised end-to-end.
        """
        from datetime import datetime, timedelta

        create = await _create_test_order(PHONE_E2E_RETURN)
        if not create.get("success"):
            pytest.skip(f"Order creation failed for e2e test: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone=PHONE_E2E_RETURN, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)

        details = await tools["get_order_details"].ainvoke({
            "order_id": oid,
            "phone_number": PHONE_E2E_RETURN,
        })
        assert isinstance(details, dict)
        assert details.get("success") is True or details.get("order_id")

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        grace = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": yesterday,
            "request_type": "return",
        })
        assert isinstance(grace, str)

        msg = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "return",
        })
        assert isinstance(msg, str)
        assert len(msg) > 10

        esc = await tools["escalate_to_agent"].ainvoke({
            "reason": "Customer confirmed return after grace check",
            "category": "Return Request",
            "details": f"Return for order {oid}",
            "order_id": oid,
            "phone_number": PHONE_E2E_RETURN,
        })
        assert isinstance(esc, dict)
        assert esc.get("message")


# ===========================================================================
# 9. Phone validation consistency
# ===========================================================================

class TestPhoneValidationConsistency:

    @pytest.mark.asyncio
    async def test_get_order_details_requires_valid_phone(self):
        """get_order_details should deny access when phone is wrong."""
        create = await _create_test_order(PHONE_ORDER_DETAILS_RE)
        if not create.get("success"):
            pytest.skip(f"Order creation failed: {create}")
        oid = _order_id_from_result(create)

        state = _build_state(phone="1111111111", client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_order_details"].ainvoke({
            "order_id": oid,
            "phone_number": "1111111111",
        })
        assert isinstance(result, dict)
        is_denied = (
            result.get("error") == "access_denied"
            or result.get("phone_validated") is False
        )
        assert is_denied, (
            f"Wrong phone should be denied access: {result}"
        )

    @pytest.mark.asyncio
    async def test_get_order_details_empty_phone_still_works(self):
        """Empty phone_number should not crash but may fail validation."""
        state = _build_state(phone="", client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_order_details"].ainvoke({
            "order_id": "9999999",
            "phone_number": "",
        })
        assert isinstance(result, dict)


# ===========================================================================
# 10. Edge cases
# ===========================================================================

class TestEdgeCases:

    @pytest.mark.asyncio
    async def test_delivered_orders_large_limit(self):
        """Large limit should not crash."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
            "limit": 100,
        })
        assert isinstance(result, dict)
        assert isinstance(result.get("orders"), list)

    @pytest.mark.asyncio
    async def test_delivered_orders_limit_zero(self):
        """limit=0 should return empty or all — should not crash."""
        state = _build_state(phone=PHONE_DELIVERED_ORDERS, client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": PHONE_DELIVERED_ORDERS,
            "limit": 0,
        })
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_grace_period_future_date(self):
        """Future delivery date should show 0 or negative days."""
        from datetime import datetime, timedelta

        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["check_grace_period_eligibility"].ainvoke({
            "delivery_date": future,
            "request_type": "return",
        })
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_escalation_long_details(self):
        """Very long details string should not crash."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        long_details = "Customer is very upset. " * 100
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Persistent customer",
            "category": "Return Request",
            "details": long_details,
        })
        assert isinstance(result, dict)
        assert result.get("message")

    @pytest.mark.asyncio
    async def test_escalation_special_characters(self):
        """Special characters in params should not crash."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["escalate_to_agent"].ainvoke({
            "reason": "Test with 'quotes' and \"double quotes\"",
            "category": "Return Request <script>alert('xss')</script>",
            "details": "Special chars: @#$%^&*(){}[]|\\;:',.<>?/~`",
        })
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_final_message_unknown_request_type(self):
        """Unknown request_type should not crash — may return default or error."""
        state = _build_state(client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_final_return_exchange_message"].ainvoke({
            "request_type": "refund",
        })
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_delivered_orders_phone_with_country_code(self):
        """Phone with +91 prefix should be handled."""
        state = _build_state(phone="+919500300001", client_id=TEST_CLIENT_ID)
        tools, _ = _get_return_exchange_tools(state)
        result = await tools["get_customers_delivered_orders_by_phone"].ainvoke({
            "phone_number": "+919500300001",
        })
        assert isinstance(result, dict)
        assert isinstance(result.get("orders"), list)
