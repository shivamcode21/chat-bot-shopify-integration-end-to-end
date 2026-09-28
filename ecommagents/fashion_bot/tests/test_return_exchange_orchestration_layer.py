from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from fashion_bot.return_partners import identity, instructions, refunds, rules, shipments, stock
from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator
from fashion_bot.return_partners.shopify_service import ShopifyReturnPartnerService


CLIENT_ID = "client-return-tests"


def _order(**overrides):
    base = {
        "id": 1001,
        "admin_graphql_api_id": "gid://shopify/Order/1001",
        "name": "#1001",
        "email": "priya@example.com",
        "phone": "+91 98765 43210",
        "fulfillment_status": "delivered",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "customer": {
            "email": "priya@example.com",
            "phone": "+91 98765 43210",
            "tags": "",
        },
        "shipping_address": {"phone": "+91 98765 43210"},
        "line_items": [
            {
                "id": "li-blue-shirt",
                "product_id": "p-shirt",
                "variant_id": "v-blue-m",
                "title": "Blue Shirt",
                "quantity": 1,
                "tags": "summer",
                "product_type": "shirt",
            }
        ],
    }
    base.update(overrides)
    return base


def _rules_config():
    return {
        "identity": {"require_customer_identity": True},
        "common": {
            "require_delivered": True,
            "tag_intact_required": True,
            "blocked_product_categories": ["innerwear"],
            "loyalty_exemption": {"enabled": True, "customer_tags": ["vip"]},
        },
        "return": {
            "window_days": 7,
            "proof_required_reasons": ["damaged", "wrong item"],
            "refund": {
                "visibility_mode": "none",
                "default_destination": "wallet",
                "wallet_sla_days": 1,
                "source_sla_business_days": 7,
                "return_fee_flat": 200,
            },
        },
        "exchange": {
            "window_days": 10,
            "require_exchange_stock": True,
            "exchange_creation_policy": "on_pickup",
        },
    }


@pytest.mark.asyncio
async def test_return_exchange_orchestrator_does_not_mutate_state(monkeypatch):
    class FakeReturnPartnerService:
        async def get_portal_link(self, client_id, order_number, **kwargs):
            return {
                "success": True,
                "eligible": True,
                "portal_url": "https://returns.example.test/start",
            }

    async def fake_identity(**kwargs):
        return {
            "success": True,
            "verified": True,
            "should_block": False,
            "order": _order(),
        }

    async def fake_validation(**kwargs):
        return {"success": True, "valid": True}

    async def fake_resolve(**kwargs):
        return "fake_partner", FakeReturnPartnerService()

    monkeypatch.setattr(ReturnPartnerOrchestrator, "_verify_identity", fake_identity)
    monkeypatch.setattr(ReturnPartnerOrchestrator, "avalidate_return_exchange_rules", fake_validation)
    monkeypatch.setattr(ReturnPartnerOrchestrator, "_resolve_service", fake_resolve)

    state = {
        "client_id": CLIENT_ID,
        "phone_number": "9876543210",
        "messages": [{"role": "user", "content": "I want to return #1001"}],
        "nested": {"existing": ["keep"]},
    }
    before = deepcopy(state)

    result = await ReturnPartnerOrchestrator.aget_return_or_exchange_portal(
        client_id=CLIENT_ID,
        order_number="#1001",
        customer_phone="9876543210",
        request_type="return",
        state=state,
    )

    assert result["success"] is True
    assert state == before


@pytest.mark.asyncio
async def test_identity_requires_phone_or_email(monkeypatch):
    async def fake_order(client_id, order_number, state):
        return _order()

    monkeypatch.setattr(identity, "_aget_order", fake_order)

    result = await identity.averify_order_identity(
        client_id=CLIENT_ID,
        order_number="#1001",
        state={},
    )

    assert result["needs_identity"] is True
    assert result["should_block"] is True
    assert result["failed_reason"] == "identity_required"


@pytest.mark.asyncio
async def test_identity_accepts_email_then_phone(monkeypatch):
    async def fake_order(client_id, order_number, state):
        return _order()

    monkeypatch.setattr(identity, "_aget_order", fake_order)

    email_result = await identity.averify_order_identity(
        client_id=CLIENT_ID,
        order_number="#1001",
        state={},
        customer_email="PRIYA@example.com",
    )
    phone_result = await identity.averify_order_identity(
        client_id=CLIENT_ID,
        order_number="#1001",
        state={},
        customer_phone="09876543210",
    )

    assert email_result["verified"] is True
    assert email_result["matched_on"] == "email"
    assert phone_result["verified"] is True
    assert phone_result["matched_on"] == "phone"


@pytest.mark.asyncio
async def test_rules_block_proof_and_tag_until_customer_confirms(monkeypatch):
    async def fake_config(config_key, client_id=None):
        return _rules_config()

    monkeypatch.setattr(rules, "aget_json_config", fake_config)

    result = await rules.avalidate_return_exchange_request(
        client_id=CLIENT_ID,
        order_number="#1001",
        request_type="return",
        order=_order(),
        return_reason="damaged product",
        proof_provided=False,
        tag_intact_confirmed=False,
    )

    assert result["valid"] is False
    assert result["needs_customer_input"] is True
    assert set(result["needs_input"]) == {"proof_required", "tag_intact_confirmation_required"}


@pytest.mark.asyncio
async def test_rules_block_category_but_loyalty_exempts_window(monkeypatch):
    async def fake_config(config_key, client_id=None):
        return _rules_config()

    monkeypatch.setattr(rules, "aget_json_config", fake_config)

    old_vip_order = _order(
        updated_at=(datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
        customer={"email": "priya@example.com", "phone": "+91 98765 43210", "tags": "vip"},
    )
    old_vip_result = await rules.avalidate_return_exchange_request(
        client_id=CLIENT_ID,
        order_number="#1001",
        request_type="return",
        order=old_vip_order,
        proof_provided=True,
        tag_intact_confirmed=True,
    )

    blocked_category_result = await rules.avalidate_return_exchange_request(
        client_id=CLIENT_ID,
        order_number="#1001",
        request_type="return",
        order=_order(line_items=[{**_order()["line_items"][0], "product_type": "innerwear"}]),
        proof_provided=True,
        tag_intact_confirmed=True,
    )

    assert old_vip_result["valid"] is True
    assert old_vip_result["details"]["loyalty_exempt"] is True
    assert blocked_category_result["valid"] is False
    assert "blocked_product_category" in blocked_category_result["failed_rules"]


@pytest.mark.asyncio
async def test_refund_visibility_is_policy_based_without_integration(monkeypatch):
    async def fake_config(config_key, client_id=None):
        return _rules_config()

    monkeypatch.setattr(refunds, "aget_json_config", fake_config)

    result = await refunds.aget_refund_visibility(
        client_id=CLIENT_ID,
        request={"approved_at": datetime.now(timezone.utc).isoformat()},
        request_type="return",
    )

    assert result["status"] == "unknown_with_sla"
    assert result["confidence"] == "policy_based"
    assert result["destination"] == "wallet"
    assert result["should_escalate"] is False


@pytest.mark.asyncio
async def test_refund_visibility_escalates_after_sla(monkeypatch):
    async def fake_config(config_key, client_id=None):
        cfg = _rules_config()
        cfg["return"]["refund"]["default_destination"] = "source"
        cfg["return"]["refund"]["source_sla_business_days"] = 1
        return cfg

    monkeypatch.setattr(refunds, "aget_json_config", fake_config)

    result = await refunds.aget_refund_visibility(
        client_id=CLIENT_ID,
        request={"approved_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()},
        request_type="return",
    )

    assert result["status"] == "sla_breached"
    assert result["should_escalate"] is True


@pytest.mark.asyncio
async def test_refund_visibility_uses_partner_report_when_configured(monkeypatch):
    async def fake_config(config_key, client_id=None):
        cfg = _rules_config()
        cfg["return"]["refund"]["visibility_mode"] = "return_partner"
        return cfg

    monkeypatch.setattr(refunds, "aget_json_config", fake_config)

    result = await refunds.aget_refund_visibility(
        client_id=CLIENT_ID,
        request={"refund": {"status": "processed", "amount": "499.00", "currency": "INR"}},
        request_type="return",
    )

    assert result["status"] == "processed"
    assert result["confidence"] == "partner_reported"
    assert result["amount"] == "499.00"


@pytest.mark.asyncio
async def test_return_pickup_uses_logistics_router_for_awb(monkeypatch):
    async def fake_tracking(awb, order_dto, state=None, fallback_to_all=False):
        assert fallback_to_all is True
        return (
            "delhivery",
            {"orders": [{"shipment_status": "In Transit"}]},
            {"delhivery": {"orders": [{"shipment_status": "In Transit"}]}},
        )

    from fashion_bot.core.logistics_router import LogisticsRouter

    monkeypatch.setattr(LogisticsRouter, "aget_tracking_first_valid", fake_tracking)

    result = await shipments.aenrich_return_pickup_leg(
        request={
            "status": "approved",
            "raw": {
                "line_items": [
                    {
                        "shipping": {
                            "awb": "AWB123",
                            "tracking_url": "https://track.delhivery.com/AWB123",
                            "carrier": "Delhivery",
                        }
                    }
                ]
            },
        },
        state={"client_id": CLIENT_ID},
    )

    assert result["partner"] == "delhivery"
    assert result["status"] == "picked_up"
    assert result["awb"] == "AWB123"


def test_resolve_request_prefers_single_request_over_multi_lists():
    # Most common case: exactly one request matched. Even if "requests"/
    # "requests_full" were somehow also present, the single "request" must win
    # and be returned untouched.
    status_result = {
        "success": True,
        "request": {"request_number": "RET1", "refund": {"status": "processed"}},
        "requests": [{"request_number": "RET2"}],
        "requests_full": [{"request_number": "RET3", "refund": {"status": "pending"}}],
    }

    request = ReturnPartnerOrchestrator._resolve_request_from_status_result(status_result)

    assert request == {"request_number": "RET1", "refund": {"status": "processed"}}


def test_resolve_request_uses_full_detail_when_multiple_requests_exist():
    # Order with 2+ return requests (e.g. two items returned separately): no
    # single "request" key, only the stripped summary list and the full
    # detail list. Must prefer the full detail, not the stripped summary.
    status_result = {
        "success": True,
        "message": "Multiple return/exchange requests found for this order.",
        "requests": [
            {"request_id": "r1", "request_number": "RET735", "status": "inspected"},
            {"request_id": "r2", "request_number": "RET736", "status": "inspected"},
        ],
        "requests_full": [
            {
                "request_id": "r1",
                "request_number": "RET735",
                "status": "inspected",
                "refund": {"status": "refunded"},
                "line_items": [{"shipping": {"awb": "24326350046292", "shipping_company": "xpressbees"}}],
            },
            {"request_id": "r2", "request_number": "RET736", "status": "inspected", "refund": {"status": "pending"}},
        ],
    }

    request = ReturnPartnerOrchestrator._resolve_request_from_status_result(status_result)

    assert request["request_number"] == "RET735"
    assert request["refund"]["status"] == "refunded"
    assert request["line_items"][0]["shipping"]["awb"] == "24326350046292"


def test_resolve_request_falls_back_to_stripped_summary_without_full_detail():
    # Defensive fallback for any caller that hasn't been updated to supply
    # "requests_full" yet — should not crash, just degrade to the old behavior.
    status_result = {
        "success": True,
        "requests": [{"request_id": "r1", "request_number": "RET735", "status": "inspected"}],
    }

    request = ReturnPartnerOrchestrator._resolve_request_from_status_result(status_result)

    assert request == {"request_id": "r1", "request_number": "RET735", "status": "inspected"}


@pytest.mark.asyncio
async def test_refund_status_uses_full_detail_when_order_has_multiple_return_requests(monkeypatch):
    # Regression test for the bug where an order with 2+ return requests
    # (e.g. GV17299 -> RET735 + RET736) lost refund_status entirely because
    # aget_refund_status picked from the field-stripped "requests" summary.
    async def fake_return_status(**kwargs):
        return {
            "success": True,
            "identity_verified": True,
            "order_name": "#1001",
            "requests": [
                {"request_id": "r1", "request_number": "RET735", "status": "inspected"},
                {"request_id": "r2", "request_number": "RET736", "status": "inspected"},
            ],
            "requests_full": [
                {
                    "request_id": "r1",
                    "request_number": "RET735",
                    "status": "inspected",
                    "refund": {"status": "processed", "amount": "499.00", "currency": "INR"},
                },
                {"request_id": "r2", "request_number": "RET736", "status": "inspected"},
            ],
        }

    async def fake_config(config_key, client_id=None):
        cfg = _rules_config()
        cfg["return"]["refund"]["visibility_mode"] = "return_partner"
        return cfg

    monkeypatch.setattr(ReturnPartnerOrchestrator, "aget_return_status", fake_return_status)
    monkeypatch.setattr(refunds, "aget_json_config", fake_config)

    result = await ReturnPartnerOrchestrator.aget_refund_status(
        client_id=CLIENT_ID,
        order_number="#1001",
        state={"client_id": CLIENT_ID},
        customer_phone="9876543210",
    )

    assert result["success"] is True
    assert result["refund"]["status"] == "processed"
    assert result["refund"]["confidence"] == "partner_reported"
    assert result["request"]["request_number"] == "RET735"


@pytest.mark.asyncio
async def test_pickup_status_uses_full_detail_when_order_has_multiple_return_requests(monkeypatch):
    # Regression test: previously aget_return_pickup_status got an empty {}
    # request whenever an order had 2+ return requests, so a genuinely
    # picked-up return always reported as "not picked up yet".
    async def fake_return_status(**kwargs):
        return {
            "success": True,
            "identity_verified": True,
            "order_name": "#1001",
            "requests": [
                {"request_id": "r1", "request_number": "RET735", "status": "inspected"},
                {"request_id": "r2", "request_number": "RET736", "status": "inspected"},
            ],
            "requests_full": [
                {
                    "request_id": "r1",
                    "request_number": "RET735",
                    "status": "inspected",
                    "raw": {
                        "line_items": [
                            {
                                "shipping": {
                                    "awb": "24326350046292",
                                    "shipping_company": "xpressbees",
                                    "tracking_url": "https://track.xpressbees.com/24326350046292",
                                }
                            }
                        ]
                    },
                },
                {"request_id": "r2", "request_number": "RET736", "status": "inspected"},
            ],
        }

    async def fake_tracking(awb, order_dto, state=None, fallback_to_all=False):
        return (
            "xpressbees",
            {"orders": [{"shipment_status": "Delivered"}]},
            {"xpressbees": {"orders": [{"shipment_status": "Delivered"}]}},
        )

    from fashion_bot.core.logistics_router import LogisticsRouter

    monkeypatch.setattr(ReturnPartnerOrchestrator, "aget_return_status", fake_return_status)
    monkeypatch.setattr(LogisticsRouter, "aget_tracking_first_valid", fake_tracking)

    result = await ReturnPartnerOrchestrator.aget_return_pickup_status(
        client_id=CLIENT_ID,
        order_number="#1001",
        state={"client_id": CLIENT_ID},
        customer_phone="9876543210",
    )

    assert result["success"] is True
    assert result["pickup"]["awb"] == "24326350046292"
    assert result["pickup"]["status"] == "return_to_origin"


@pytest.mark.asyncio
async def test_return_pickup_logistics_falls_back_after_primary_miss(monkeypatch):
    from fashion_bot.core import logistics_router
    from fashion_bot.core.logistics_router import LogisticsRouter
    import fashion_bot.utils.delivery_partner_utils as delivery_partner_utils

    class FakeAdapter:
        def __init__(self, partner):
            self.partner = partner

        async def aget_tracking_details(self, awb, state=None):
            if self.partner == "shiprocket":
                return {"success": True, "found": False}
            return {"awb": awb, "current_location": "Bengaluru"}

    async def fake_route(order_dto, state=None):
        return ["shiprocket"]

    async def fake_integrated(state=None):
        return ["shiprocket", "delhivery", "bluedart"]

    async def fake_adapter(partner, state=None):
        return FakeAdapter(partner)

    monkeypatch.setattr(LogisticsRouter, "aroute_for_order", fake_route)
    monkeypatch.setattr(delivery_partner_utils, "aget_integrated_partners", fake_integrated)
    monkeypatch.setattr(logistics_router, "_aget_adapter_for", fake_adapter)

    partner, result, per_partner = await LogisticsRouter.aget_tracking_first_valid(
        "AWB123",
        {"tracking_company": "Shiprocket"},
        state={"client_id": CLIENT_ID},
        fallback_to_all=True,
    )

    assert partner == "delhivery"
    assert result["awb"] == "AWB123"
    assert list(per_partner) == ["shiprocket", "delhivery"]


def test_chat_use_case_fixture_covers_journey_categories():
    path = Path(__file__).parent / "fixtures" / "return_exchange_chat_use_cases.json"
    data = json.loads(path.read_text())
    use_case_ids = {case["id"] for case in data["use_cases"]}

    expected = {
        "identity_email_success",
        "identity_missing",
        "not_delivered",
        "outside_window",
        "blocked_category",
        "wallet_fee_policy",
        "proof_required",
        "tag_intact_required",
        "loyalty_exemption",
        "return_pickup_tracking",
        "multi_courier_return_tracking",
        "exchange_forward_tracking",
        "refund_within_sla",
        "refund_sla_breached",
        "partial_item_return",
        "shopify_native_config",
    }

    assert expected.issubset(use_case_ids)


@pytest.mark.asyncio
async def test_exchange_delivery_status_fetches_forward_order_when_exchange_exists(monkeypatch):
    async def fake_return_status(**kwargs):
        return {
            "success": True,
            "identity_verified": True,
            "request": {
                "request_type": "exchange",
                "request_number": "RP-1",
                "exchange_order": {"name": "EX-1001", "id": "gid://shopify/Order/2001"},
            },
        }

    async def fake_order_status(order_id, state=None):
        return {"total_orders_found": 1, "orders": [{"order_name": order_id, "status": "in_transit"}]}

    from fashion_bot.core.orchestrator import OrderStatusOrchestrator

    monkeypatch.setattr(ReturnPartnerOrchestrator, "aget_return_status", fake_return_status)
    monkeypatch.setattr(OrderStatusOrchestrator, "aget_order_status", fake_order_status)

    result = await ReturnPartnerOrchestrator.aget_exchange_delivery_status(
        client_id=CLIENT_ID,
        order_number="#1001",
        state={"client_id": CLIENT_ID},
        customer_phone="9876543210",
    )

    assert result["success"] is True
    assert result["exchange_order_exists"] is True
    assert result["exchange_order"]["name"] == "EX-1001"
    assert result["exchange_delivery_status"]["orders"][0]["status"] == "in_transit"


@pytest.mark.asyncio
async def test_exchange_delivery_status_reports_not_created_without_forward_lookup(monkeypatch):
    async def fake_return_status(**kwargs):
        return {
            "success": True,
            "identity_verified": True,
            "request": {"request_type": "exchange", "request_number": "RP-2"},
        }

    monkeypatch.setattr(ReturnPartnerOrchestrator, "aget_return_status", fake_return_status)

    result = await ReturnPartnerOrchestrator.aget_exchange_delivery_status(
        client_id=CLIENT_ID,
        order_number="#1001",
        state={"client_id": CLIENT_ID},
        customer_phone="9876543210",
    )

    assert result["success"] is True
    assert result["exchange_order_exists"] is False
    assert "not been created yet" in result["message"]


@pytest.mark.asyncio
async def test_exchange_delivery_status_escalates_after_pickup_without_exchange_order(monkeypatch):
    async def fake_return_status(**kwargs):
        return {
            "success": True,
            "identity_verified": True,
            "request": {
                "request_id": "rp-request-1",
                "request_type": "exchange",
                "request_number": "EXC-1",
                "raw": {
                    "line_items": [
                        {
                            "quantity": 1,
                            "exchange": {"variant_id": "1234567890"},
                        }
                    ]
                },
            },
        }

    async def fake_pickup_leg(**kwargs):
        return {
            "success": True,
            "leg": "return_pickup",
            "status": "picked_up",
            "message": "Pickup is completed.",
        }

    async def fake_stock_check(**kwargs):
        return {
            "success": True,
            "stock_available": False,
            "message": "Requested exchange item stock is not currently available.",
            "variants": [{"variant_id": "gid://shopify/ProductVariant/1234567890", "available": False}],
        }

    async def fake_instruction_config(**kwargs):
        return {"support_email": "support@groovee.in"}

    escalations = []

    async def fake_escalation(**kwargs):
        escalations.append(kwargs)
        return "esc-123"

    monkeypatch.setattr(ReturnPartnerOrchestrator, "aget_return_status", fake_return_status)
    monkeypatch.setattr(shipments, "aenrich_return_pickup_leg", fake_pickup_leg)
    monkeypatch.setattr(stock, "acheck_shopify_variant_stock", fake_stock_check)
    monkeypatch.setattr(
        "fashion_bot.return_partners.instructions.aget_return_prime_instruction_config",
        fake_instruction_config,
    )
    monkeypatch.setattr(ReturnPartnerOrchestrator, "_raise_system_escalation", fake_escalation)

    result = await ReturnPartnerOrchestrator.aget_exchange_delivery_status(
        client_id=CLIENT_ID,
        order_number="#1001",
        state={"client_id": CLIENT_ID},
        customer_phone="9876543210",
    )

    assert result["success"] is True
    assert result["exchange_order_exists"] is False
    assert result["requires_manual_intervention"] is True
    assert result["escalation_id"] == "esc-123"
    assert result["stock"]["stock_available"] is False
    assert "support@groovee.in" in result["message"]
    assert escalations[0]["category"] == "Exchange Order Not Created"


@pytest.mark.asyncio
async def test_return_exchange_request_instructions_are_config_backed(monkeypatch):
    async def fake_json_config(config_key, client_id=None):
        if config_key == "return_prime_return_exchange_rules":
            return {
                "exchange": {"window_days": 7},
                "customer_instructions": {
                    "support_email": "support@groovee.in",
                    "approval_sla": "24-48 hrs",
                },
            }
        if config_key == "return_prime_details":
            return {"portal_url": "https://groovee.in/apps/return_prime"}
        return {}

    monkeypatch.setattr(instructions, "aget_json_config", fake_json_config)

    result = await ReturnPartnerOrchestrator.aget_return_exchange_instructions(
        client_id=CLIENT_ID,
        request_type="exchange",
        state={"client_id": CLIENT_ID},
    )

    assert result["success"] is True
    assert result["portal_url"] == "https://groovee.in/apps/return_prime"
    assert result["window_days"] == 7
    assert "approved/rejected in 24-48 hrs" in result["message"]
    assert "within 7 days of delivery" in result["message"]


@pytest.mark.asyncio
async def test_shopify_native_return_create_when_enabled_for_single_item(monkeypatch):
    service = ShopifyReturnPartnerService()

    async def fake_config(config_key, client_id=None):
        return {"shopify_native": {"create_return_enabled": True}}

    async def fake_fulfillment_line_items(client_id, order_gid_or_id):
        return {
            "success": True,
            "data": {
                "order": {
                    "fulfillments": [
                        {
                            "fulfillmentLineItems": {
                                "nodes": [
                                    {
                                        "id": "gid://shopify/FulfillmentLineItem/1",
                                        "quantity": 1,
                                        "lineItem": {
                                            "id": "gid://shopify/LineItem/1",
                                            "name": "Blue Shirt",
                                            "quantity": 1,
                                            "variant": {"id": "gid://shopify/ProductVariant/1", "title": "M"},
                                            "product": {"id": "gid://shopify/Product/1"},
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        }

    async def fake_create_return(
        client_id,
        order_gid_or_id,
        return_line_items,
        exchange_line_items=None,
        notify_customer=False,
    ):
        assert exchange_line_items is None
        assert return_line_items == [
            {
                "fulfillmentLineItemId": "gid://shopify/FulfillmentLineItem/1",
                "quantity": 1,
                "returnReason": "DEFECTIVE",
                "returnReasonNote": "damaged product",
            }
        ]
        return {
            "success": True,
            "return": {
                "id": "gid://shopify/Return/1",
                "name": "RET-1",
                "status": "REQUESTED",
            },
        }

    import fashion_bot.config_manager as config_manager
    import fashion_bot.shopify.modules.return_apis as return_apis

    monkeypatch.setattr(config_manager, "aget_json_config", fake_config)
    monkeypatch.setattr(return_apis, "aget_order_fulfillment_line_items", fake_fulfillment_line_items)
    monkeypatch.setattr(return_apis, "acreate_shopify_return", fake_create_return)

    result = await service.get_portal_link(
        CLIENT_ID,
        "#1001",
        request_type="return",
        order={"id": "gid://shopify/Order/1001", "name": "#1001"},
        return_reason="damaged product",
    )

    assert result["success"] is True
    assert result["created"] is True
    assert result["request"]["request_number"] == "RET-1"


@pytest.mark.asyncio
async def test_shopify_native_return_create_requires_selection_for_multiple_items(monkeypatch):
    service = ShopifyReturnPartnerService()

    async def fake_config(config_key, client_id=None):
        return {"shopify_native": {"create_return_enabled": True}}

    async def fake_fulfillment_line_items(client_id, order_gid_or_id):
        return {
            "success": True,
            "data": {
                "order": {
                    "fulfillments": [
                        {
                            "fulfillmentLineItems": {
                                "nodes": [
                                    {
                                        "id": "gid://shopify/FulfillmentLineItem/1",
                                        "quantity": 1,
                                        "lineItem": {"id": "li-1", "name": "Blue Shirt"},
                                    },
                                    {
                                        "id": "gid://shopify/FulfillmentLineItem/2",
                                        "quantity": 1,
                                        "lineItem": {"id": "li-2", "name": "Black Pants"},
                                    },
                                ]
                            },
                        }
                    ]
                }
            },
        }

    import fashion_bot.config_manager as config_manager
    import fashion_bot.shopify.modules.return_apis as return_apis

    monkeypatch.setattr(config_manager, "aget_json_config", fake_config)
    monkeypatch.setattr(return_apis, "aget_order_fulfillment_line_items", fake_fulfillment_line_items)

    result = await service.get_portal_link(
        CLIENT_ID,
        "#1001",
        request_type="return",
        order={"id": "gid://shopify/Order/1001", "name": "#1001"},
    )

    assert result["success"] is True
    assert result["needs_item_selection"] is True
    assert len(result["items"]) == 2


@pytest.mark.asyncio
async def test_shopify_native_exchange_create_when_enabled(monkeypatch):
    service = ShopifyReturnPartnerService()

    async def fake_config(config_key, client_id=None):
        return {"shopify_native": {"create_exchange_enabled": True}}

    async def fake_fulfillment_line_items(client_id, order_gid_or_id):
        return {
            "success": True,
            "data": {
                "order": {
                    "fulfillments": [
                        {
                            "fulfillmentLineItems": {
                                "nodes": [
                                    {
                                        "id": "gid://shopify/FulfillmentLineItem/1",
                                        "quantity": 1,
                                        "lineItem": {
                                            "id": "gid://shopify/LineItem/1",
                                            "name": "Blue Shirt",
                                            "quantity": 1,
                                            "variant": {"id": "gid://shopify/ProductVariant/1", "title": "M"},
                                            "product": {"id": "gid://shopify/Product/1"},
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        }

    async def fake_create_return(
        client_id,
        order_gid_or_id,
        return_line_items,
        exchange_line_items=None,
        notify_customer=False,
    ):
        assert return_line_items == [
            {
                "fulfillmentLineItemId": "gid://shopify/FulfillmentLineItem/1",
                "quantity": 1,
                "returnReason": "OTHER",
                "returnReasonNote": "size exchange",
            }
        ]
        assert exchange_line_items == [
            {
                "variantId": "gid://shopify/ProductVariant/2",
                "quantity": 1,
            }
        ]
        return {
            "success": True,
            "return": {
                "id": "gid://shopify/Return/2",
                "name": "EXC-1",
                "status": "REQUESTED",
                "exchangeLineItems": {
                    "nodes": [
                        {
                            "id": "gid://shopify/ExchangeLineItem/1",
                            "quantity": 1,
                            "variantId": "gid://shopify/ProductVariant/2",
                        }
                    ]
                },
            },
        }

    import fashion_bot.config_manager as config_manager
    import fashion_bot.shopify.modules.return_apis as return_apis

    monkeypatch.setattr(config_manager, "aget_json_config", fake_config)
    monkeypatch.setattr(return_apis, "aget_order_fulfillment_line_items", fake_fulfillment_line_items)
    monkeypatch.setattr(return_apis, "acreate_shopify_return", fake_create_return)

    result = await service.get_portal_link(
        CLIENT_ID,
        "#1001",
        request_type="exchange",
        order={"id": "gid://shopify/Order/1001", "name": "#1001"},
        selected_line_items=[
            {
                "variant_id": "gid://shopify/ProductVariant/1",
                "exchange_variant_id": "gid://shopify/ProductVariant/2",
            }
        ],
        return_reason="size exchange",
    )

    assert result["success"] is True
    assert result["created"] is True
    assert result["request"]["request_type"] == "exchange"
    assert result["request"]["request_number"] == "EXC-1"


@pytest.mark.asyncio
async def test_shopify_native_exchange_requires_replacement_variant(monkeypatch):
    service = ShopifyReturnPartnerService()

    async def fake_config(config_key, client_id=None):
        return {"shopify_native": {"create_exchange_enabled": True}}

    async def fake_fulfillment_line_items(client_id, order_gid_or_id):
        return {
            "success": True,
            "data": {
                "order": {
                    "fulfillments": [
                        {
                            "fulfillmentLineItems": {
                                "nodes": [
                                    {
                                        "id": "gid://shopify/FulfillmentLineItem/1",
                                        "quantity": 1,
                                        "lineItem": {"id": "li-1", "name": "Blue Shirt"},
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        }

    import fashion_bot.config_manager as config_manager
    import fashion_bot.shopify.modules.return_apis as return_apis

    monkeypatch.setattr(config_manager, "aget_json_config", fake_config)
    monkeypatch.setattr(return_apis, "aget_order_fulfillment_line_items", fake_fulfillment_line_items)

    result = await service.get_portal_link(
        CLIENT_ID,
        "#1001",
        request_type="exchange",
        order={"id": "gid://shopify/Order/1001", "name": "#1001"},
    )

    assert result["success"] is True
    assert result["needs_exchange_variant_selection"] is True
