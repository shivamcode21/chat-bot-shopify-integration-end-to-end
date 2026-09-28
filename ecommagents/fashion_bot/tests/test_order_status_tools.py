"""
Integration tests for Delhivery and Shiprocket logistics adapters.

Validates that both adapters return the expected canonical field schema
for known live orders against the real production Groovee client
(c3ffcb1b-afb9-4ca4-8746-a06698bec870). Credentials are loaded from
the postgres `client_configs` table — no hardcoded secrets here.

Orders under test:
  - gv15361  → fulfilled via Delhivery   (AWB 55434610000011)
  - gv13064  → fulfilled via Shiprocket

Also includes pure-unit tests for the `resolve_partner_alias` helper
and the `aget_partners_for_order` routing function that were changed as
part of the prefix-matching fix (BlueDart Surface 2KG regression).

Coverage:
  - Delhivery: aget_order_data schema, aget_tracking_details schema
  - Shiprocket: aget_order_data schema, aget_tracking_details schema
  - resolve_partner_alias: exact match, prefix match, unknown, empty
  - aget_partners_for_order: routes to single partner when tracking_company known
"""

import pytest
from fashion_bot.env_loader import bootstrap_environment

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"

# Delhivery order — tracking_company = "Delhivery" (exact alias match)
DELHIVERY_ORDER_ID = "gv15361"
DELHIVERY_AWB = "55434610000011"

# Shiprocket order — tracking_company = "BlueDart Surface 2KG" (prefix alias match)
SHIPROCKET_ORDER_ID = "gv13064"

# Canonical `order_data` sub-fields we must always receive from both adapters.
REQUIRED_SHIPMENTS_KEYS = {"awb", "courier", "tracking_url"}
REQUIRED_ORDER_DATA_KEYS = {
    "shipments",
    "billing_customer_name",
    "billing_phone",
}


# ---------------------------------------------------------------------------
# Session-scoped bootstrap (load .env / config once)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _bootstrap():
    bootstrap_environment()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_state(client_id: str = CLIENT_ID) -> dict:
    return {
        "client_id": client_id,
        "phone_number": "9716336096",
        "conversation_context": {"entities": [], "topics": [], "focal_entity": None},
        "messages": [],
    }


async def _get_delhivery_adapter(state: dict):
    from fashion_bot.delhivery.tools.logistics_adapter import DelhiveryLogisticsAdapter
    return await DelhiveryLogisticsAdapter.create(client_id=state["client_id"])


async def _get_shiprocket_adapter(state: dict):
    from fashion_bot.shiprocket.tools.logistics_adapter import ShiprocketLogisticsAdapter
    return await ShiprocketLogisticsAdapter.create(client_id=state["client_id"])


# ===========================================================================
# Unit tests — resolve_partner_alias (pure, no network)
# ===========================================================================

class TestResolvePartnerAlias:
    """Pure unit tests for the prefix-matching alias resolver."""

    def test_exact_match_delhivery(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("Delhivery") == "delhivery"

    def test_exact_match_shiprocket(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("Shiprocket") == "shiprocket"

    def test_exact_match_shiprocket_assigned(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("Shiprocket Assigned") == "shiprocket"

    def test_prefix_match_bluedart_surface_2kg(self):
        """The regression case: Shopify sets 'BlueDart Surface 2KG'."""
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("BlueDart Surface 2KG") == "shiprocket"

    def test_prefix_match_bluedart_express(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("BlueDart Express") == "shiprocket"

    def test_prefix_match_ecom_express_heavy(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("Ecom Express Heavy") == "shiprocket"

    def test_prefix_match_xpressbees_variant(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("Xpressbees Surface") == "shiprocket"

    def test_prefix_match_dtdc_variant(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("DTDC Express") == "shiprocket"

    def test_case_insensitive(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("DELHIVERY") == "delhivery"
        assert resolve_partner_alias("delhivery") == "delhivery"
        assert resolve_partner_alias("bluedart") == "shiprocket"
        assert resolve_partner_alias("BLUEDART SURFACE 2KG") == "shiprocket"

    def test_unknown_carrier_returns_none(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("FedEx") is None
        assert resolve_partner_alias("Unknown Courier XYZ") is None

    def test_empty_string_returns_none(self):
        from fashion_bot.core.vendor_config import resolve_partner_alias
        assert resolve_partner_alias("") is None
        assert resolve_partner_alias(None) is None
        assert resolve_partner_alias("   ") is None


# ===========================================================================
# Unit tests — aget_partners_for_order routing (mocked DB, no network)
# ===========================================================================

class TestAgetPartnersForOrder:
    """Verify routing returns a single partner for fulfilled orders whose
    tracking_company is resolvable, and falls back to all partners when not."""

    async def test_delhivery_order_routes_to_delhivery_only(self, monkeypatch):
        from fashion_bot.utils.delivery_partner_utils import aget_partners_for_order

        monkeypatch.setattr(
            "fashion_bot.utils.delivery_partner_utils.aget_integrated_partners",
            lambda **_kw: _async_return(["delhivery", "shiprocket"]),
        )

        order_dto = {"tracking_company": "Delhivery", "fulfillment_status": "fulfilled"}
        partners = await aget_partners_for_order(order_dto, state=_build_state())
        assert partners == ["delhivery"], (
            f"Expected ['delhivery'] but got {partners!r}"
        )

    async def test_bluedart_surface_2kg_routes_to_shiprocket_only(self, monkeypatch):
        """Regression: 'BlueDart Surface 2KG' must not fan-out to both partners."""
        from fashion_bot.utils.delivery_partner_utils import aget_partners_for_order

        monkeypatch.setattr(
            "fashion_bot.utils.delivery_partner_utils.aget_integrated_partners",
            lambda **_kw: _async_return(["delhivery", "shiprocket"]),
        )

        order_dto = {"tracking_company": "BlueDart Surface 2KG", "fulfillment_status": "fulfilled"}
        partners = await aget_partners_for_order(order_dto, state=_build_state())
        assert partners == ["shiprocket"], (
            f"Expected ['shiprocket'] for 'BlueDart Surface 2KG' but got {partners!r}"
        )

    async def test_unknown_carrier_fans_out_to_all_partners(self, monkeypatch):
        from fashion_bot.utils.delivery_partner_utils import aget_partners_for_order

        monkeypatch.setattr(
            "fashion_bot.utils.delivery_partner_utils.aget_integrated_partners",
            lambda **_kw: _async_return(["delhivery", "shiprocket"]),
        )

        order_dto = {"tracking_company": "FedEx", "fulfillment_status": "fulfilled"}
        partners = await aget_partners_for_order(order_dto, state=_build_state())
        assert set(partners) == {"delhivery", "shiprocket"}, (
            f"Unknown carrier should fan out to all partners, got {partners!r}"
        )

    async def test_missing_carrier_fans_out_to_all_partners(self, monkeypatch):
        from fashion_bot.utils.delivery_partner_utils import aget_partners_for_order

        monkeypatch.setattr(
            "fashion_bot.utils.delivery_partner_utils.aget_integrated_partners",
            lambda **_kw: _async_return(["delhivery", "shiprocket"]),
        )

        order_dto = {"fulfillment_status": "fulfilled"}  # no tracking_company
        partners = await aget_partners_for_order(order_dto, state=_build_state())
        assert set(partners) == {"delhivery", "shiprocket"}, (
            f"Missing carrier should fan out to all partners, got {partners!r}"
        )

    async def test_no_integrated_partners_returns_empty(self, monkeypatch):
        from fashion_bot.utils.delivery_partner_utils import aget_partners_for_order

        monkeypatch.setattr(
            "fashion_bot.utils.delivery_partner_utils.aget_integrated_partners",
            lambda **_kw: _async_return([]),
        )

        order_dto = {"tracking_company": "Delhivery"}
        partners = await aget_partners_for_order(order_dto, state=_build_state())
        assert partners == []


async def _async_return(value):
    return value


# ===========================================================================
# Integration tests — Delhivery (live API, real creds from DB)
# ===========================================================================

@pytest.mark.integration
class TestDelhiveryAdapter:
    """Live integration tests for DelhiveryLogisticsAdapter against gv15361."""

    async def test_aget_order_data_found(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        assert result.get("success") is True, (
            f"Expected success=True, got: {result}"
        )
        assert result.get("found") is True, (
            f"Expected found=True for {DELHIVERY_ORDER_ID}, got: {result}"
        )

    async def test_aget_order_data_returns_order_id(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        assert result.get("order_id"), "order_id must be non-empty"

    async def test_aget_order_data_returns_status(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        status = result.get("status")
        assert status and isinstance(status, str), (
            f"Expected a non-empty status string, got {status!r}"
        )

    async def test_aget_order_data_returns_logistics_order_id_awb(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        awb = result.get("logistics_order_id")
        assert awb, "logistics_order_id (AWB) must be non-empty"
        assert awb == DELHIVERY_AWB, (
            f"Expected AWB {DELHIVERY_AWB!r}, got {awb!r}"
        )

    async def test_aget_order_data_canonical_order_data_keys(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        order_data = result.get("order_data") or {}
        missing = REQUIRED_ORDER_DATA_KEYS - set(order_data.keys())
        assert not missing, (
            f"order_data missing required keys: {missing}. Got keys: {set(order_data.keys())}"
        )

    async def test_aget_order_data_shipments_sub_schema(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        shipments = (result.get("order_data") or {}).get("shipments") or {}
        missing = REQUIRED_SHIPMENTS_KEYS - set(shipments.keys())
        assert not missing, (
            f"shipments missing required keys: {missing}. Got: {shipments}"
        )

    async def test_aget_order_data_shipments_awb_matches(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        awb = (result.get("order_data") or {}).get("shipments", {}).get("awb")
        assert awb == DELHIVERY_AWB, (
            f"shipments.awb expected {DELHIVERY_AWB!r}, got {awb!r}"
        )

    async def test_aget_order_data_shipments_courier_is_delhivery(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        courier = (result.get("order_data") or {}).get("shipments", {}).get("courier", "")
        assert "delhivery" in courier.lower(), (
            f"Expected courier to contain 'delhivery', got {courier!r}"
        )

    async def test_aget_order_data_shipments_tracking_url_contains_awb(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        url = (result.get("order_data") or {}).get("shipments", {}).get("tracking_url", "")
        assert DELHIVERY_AWB in url, (
            f"tracking_url should contain AWB {DELHIVERY_AWB!r}, got {url!r}"
        )

    async def test_aget_order_data_billing_customer_name_non_empty(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        name = (result.get("order_data") or {}).get("billing_customer_name", "")
        assert name and name.strip(), (
            f"billing_customer_name must be non-empty, got {name!r}"
        )

    async def test_aget_order_data_shipments_top_level_shortcut(self):
        """The adapter also exposes shipments at result['shipments'] for convenience."""
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data(DELHIVERY_ORDER_ID, state=state)

        top_shipments = result.get("shipments") or {}
        assert top_shipments.get("awb"), (
            f"result['shipments']['awb'] must be set. Got: {top_shipments}"
        )

    async def test_aget_tracking_details_by_awb(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_tracking_details(DELHIVERY_AWB, state=state)

        assert result.get("awb") == DELHIVERY_AWB, (
            f"Expected awb={DELHIVERY_AWB!r} in tracking result, got {result}"
        )

    async def test_aget_tracking_details_has_status(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_tracking_details(DELHIVERY_AWB, state=state)

        status = result.get("status")
        assert status and isinstance(status, str), (
            f"Tracking result must have a non-empty 'status', got {result}"
        )
        assert result.get("status") not in ("error",), (
            f"Tracking call returned error: {result}"
        )

    async def test_aget_tracking_details_has_current_location(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_tracking_details(DELHIVERY_AWB, state=state)

        assert "current_location" in result, (
            f"Tracking result missing 'current_location'. Got keys: {list(result.keys())}"
        )

    async def test_aget_tracking_details_has_latest_activity(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_tracking_details(DELHIVERY_AWB, state=state)

        assert "latest_activity" in result, (
            f"Tracking result missing 'latest_activity'. Got keys: {list(result.keys())}"
        )

    async def test_unknown_order_returns_not_found(self):
        state = _build_state()
        adapter = await _get_delhivery_adapter(state)
        result = await adapter.aget_order_data("NONEXISTENT_ORDER_XYZ_99999", state=state)

        assert result.get("found") is False, (
            f"Expected found=False for unknown order, got {result}"
        )


# ===========================================================================
# Integration tests — Shiprocket (live API, real creds from DB)
# ===========================================================================

@pytest.mark.integration
class TestShiprocketAdapter:
    """Live integration tests for ShiprocketLogisticsAdapter against gv13064."""

    async def test_aget_order_data_found(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        assert result.get("success") is True, (
            f"Expected success=True, got: {result}"
        )
        assert result.get("found") is True, (
            f"Expected found=True for {SHIPROCKET_ORDER_ID}, got: {result}"
        )

    async def test_aget_order_data_returns_order_id(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        assert result.get("order_id"), "order_id must be non-empty"

    async def test_aget_order_data_returns_status(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        status = result.get("status")
        assert status and isinstance(status, str), (
            f"Expected a non-empty status string, got {status!r}"
        )

    async def test_aget_order_data_returns_logistics_order_id(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        assert result.get("logistics_order_id"), (
            "logistics_order_id (Shiprocket order ID) must be non-empty"
        )

    async def test_aget_order_data_canonical_order_data_keys(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        order_data = result.get("order_data") or {}
        missing = REQUIRED_ORDER_DATA_KEYS - set(order_data.keys())
        assert not missing, (
            f"order_data missing required keys: {missing}. Got keys: {set(order_data.keys())}"
        )

    async def test_aget_order_data_shipments_sub_schema(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        shipments = (result.get("order_data") or {}).get("shipments") or {}
        missing = REQUIRED_SHIPMENTS_KEYS - set(shipments.keys())
        assert not missing, (
            f"shipments missing required keys: {missing}. Got: {shipments}"
        )

    async def test_aget_order_data_shipments_awb_non_empty(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        awb = (result.get("order_data") or {}).get("shipments", {}).get("awb")
        assert awb, f"shipments.awb must be non-empty, got {awb!r}"

    async def test_aget_order_data_shipments_courier_non_empty(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        courier = (result.get("order_data") or {}).get("shipments", {}).get("courier", "")
        assert courier and courier != "N/A", (
            f"shipments.courier must be non-empty/non-N/A, got {courier!r}"
        )

    async def test_aget_order_data_shipments_tracking_url_non_empty(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        url = (result.get("order_data") or {}).get("shipments", {}).get("tracking_url", "")
        assert url and url.startswith("http"), (
            f"shipments.tracking_url must be a URL, got {url!r}"
        )

    async def test_aget_order_data_billing_customer_name_non_empty(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        name = (result.get("order_data") or {}).get("billing_customer_name", "")
        assert name and name.strip() and name != "Guest", (
            f"billing_customer_name must be non-empty and not 'Guest', got {name!r}"
        )

    async def test_aget_order_data_shipments_top_level_shortcut(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)

        top_shipments = result.get("shipments") or {}
        assert top_shipments.get("awb"), (
            f"result['shipments']['awb'] must be set. Got: {top_shipments}"
        )

    async def test_aget_tracking_details_by_awb(self):
        """Fetch the AWB from aget_order_data first, then track it."""
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        order_result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)
        awb = (order_result.get("order_data") or {}).get("shipments", {}).get("awb")
        assert awb, f"Could not resolve AWB for {SHIPROCKET_ORDER_ID} to run tracking test"

        result = await adapter.aget_tracking_details(awb, state=state)
        assert result.get("awb") == awb, (
            f"Tracking result awb mismatch: expected {awb!r}, got {result.get('awb')!r}"
        )

    async def test_aget_tracking_details_has_status(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        order_result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)
        awb = (order_result.get("order_data") or {}).get("shipments", {}).get("awb")
        assert awb, f"Could not resolve AWB for {SHIPROCKET_ORDER_ID}"

        result = await adapter.aget_tracking_details(awb, state=state)
        status = result.get("status")
        assert status and isinstance(status, str), (
            f"Tracking result must have a non-empty 'status', got {result}"
        )
        assert status not in ("error",), f"Tracking call returned error: {result}"

    async def test_aget_tracking_details_has_current_location(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        order_result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)
        awb = (order_result.get("order_data") or {}).get("shipments", {}).get("awb")
        assert awb, f"Could not resolve AWB for {SHIPROCKET_ORDER_ID}"

        result = await adapter.aget_tracking_details(awb, state=state)
        assert "current_location" in result, (
            f"Tracking result missing 'current_location'. Got keys: {list(result.keys())}"
        )

    async def test_aget_tracking_details_has_latest_activity(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        order_result = await adapter.aget_order_data(SHIPROCKET_ORDER_ID, state=state)
        awb = (order_result.get("order_data") or {}).get("shipments", {}).get("awb")
        assert awb, f"Could not resolve AWB for {SHIPROCKET_ORDER_ID}"

        result = await adapter.aget_tracking_details(awb, state=state)
        assert "latest_activity" in result, (
            f"Tracking result missing 'latest_activity'. Got keys: {list(result.keys())}"
        )

    async def test_unknown_order_returns_not_found(self):
        state = _build_state()
        adapter = await _get_shiprocket_adapter(state)
        result = await adapter.aget_order_data("NONEXISTENT_ORDER_XYZ_99999", state=state)

        assert result.get("found") is False, (
            f"Expected found=False for unknown order, got {result}"
        )
