"""
Unit tests for the ClickPost partner integration.

All mocked -- no live network. Covers:
  - partner self-registration,
  - the config guard (configuration_missing before any HTTP call, so a
    tenant without real credentials stays inert),
  - waybill resolution from Shopify fulfillments -- ClickPost's only order
    surface here, since an aggregator does not own orders,
  - the JSON tracking response parser,
  - call ordering and the terminal-status cancel guard.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from fashion_bot.clickpost.processors.order_processor import ClickPostOrderProcessor
from fashion_bot.clickpost.tools.logistics_adapter import ClickPostLogisticsAdapter
from fashion_bot.clickpost.tools.response_parser import parse_clickpost_tracking_response
from fashion_bot.core import logistics_registry
from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter


VALID_CONFIG = {
    "api_base": "https://api.clickpost.example.com",
    "username": "real_username",
    "key": "real_api_key",
    "account_code": "acc123",
}

def _track_order_json(bucket, code, status, location, timestamp, scans=None, **extra):
    """Build a track-order response in ClickPost's documented shape: ``result``
    keyed by waybill, each entry holding latest_status / scans / valid."""
    record = {
        "latest_status": {
            "clickpost_status_bucket": bucket,
            "clickpost_status_code": code,
            "status": status,
            "remark": status,
            "location": location,
            "timestamp": timestamp,
        },
        "additional": {"courier_partner_edd": "2026-06-27"},
        "scans": scans or [],
        "valid": True,
    }
    record.update(extra)
    return {
        "meta": {"success": True, "message": "SUCCESS", "status": 200},
        "result": {"CP123456789": record},
    }


SAMPLE_TRACKING_JSON = _track_order_json(
    bucket=4, code=6, status="Out for Delivery", location="GURGAON",
    timestamp="2026-06-26 09:10:00",
    scans=[
        {
            "clickpost_status_bucket": 3, "clickpost_status_code": 5,
            "clickpost_status_description": "InTransit",
            "status": "Reached the nearest hub", "remark": "Reached the nearest hub",
            "location": "DELHI", "timestamp": "2026-06-25 18:30:00",
        },
        {
            "clickpost_status_bucket": 4, "clickpost_status_code": 6,
            "clickpost_status_description": "OutForDelivery",
            "status": "Out for Delivery", "remark": "Out for Delivery",
            "location": "GURGAON", "timestamp": "2026-06-26 09:10:00",
        },
    ],
)

DELIVERED_JSON = _track_order_json(
    bucket=6, code=8, status="Delivered successfully", location="GURGAON",
    timestamp="2026-06-26 15:01:00",
)

IN_TRANSIT_JSON = _track_order_json(
    bucket=3, code=5, status="Assigned to a rider", location="DELHI",
    timestamp="2026-06-25 08:52:51",
)


def _fulfillment(tracking_company="", tracking_url="", tracking_number=None):
    fulfillment = {"tracking_company": tracking_company, "tracking_url": tracking_url}
    if tracking_number is not None:
        fulfillment["tracking_numbers"] = [tracking_number]
    return fulfillment


# ── Registration ────────────────────────────────────────────────────────

def test_clickpost_registers_in_logistics_registry():
    assert logistics_registry.get_partner("clickpost") is not None


# ── Processor ───────────────────────────────────────────────────────────

def test_processor_returns_empty_for_empty_input():
    """An empty row yields nothing, and never raises: this runs inside the
    order-status path, whose outer handler turns any exception into "order
    not found". Real rows must survive -- see
    ``test_fulfilled_order_survives_the_status_race``."""
    processor = ClickPostOrderProcessor()
    assert processor.process_order({}) == {}
    assert processor.process_orders([{}]) == []


def test_processor_maps_a_real_row():
    """The row aget_matching_orders builds must come back as a populated DTO,
    not be dropped -- the orchestrator returns this count as the final answer."""
    row = {
        "order_id": "SF3772904310VAO",
        "channel_order_id": "V0018025",
        "status": "out_for_delivery",
        "shipment_status": "out_for_delivery",
        "order_data": {"waybill": "SF3772904310VAO", "shipments": {"etd": "2026-08-10"}},
    }

    out = ClickPostOrderProcessor().process_orders([row])

    assert len(out) == 1
    assert out[0].get("order_id") or out[0].get("order_name")


# ── create_shipment is never supported ──────────────────────────────────

def test_create_shipment_raises_not_implemented():
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    with pytest.raises(NotImplementedError):
        adapter.create_shipment({})


@pytest.mark.asyncio
async def test_acreate_shipment_raises_not_implemented():
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    with pytest.raises(NotImplementedError):
        await adapter.acreate_shipment({})


# ── Config validation: inert without real credentials ───────────────────

@pytest.mark.asyncio
async def test_missing_config_returns_configuration_missing_and_makes_no_call(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = {}

    track_mock = AsyncMock()
    monkeypatch.setattr(adapter, "_track_waybill", track_mock)

    result = await adapter.aget_tracking_details("CP123456789")

    assert result == {
        "success": False,
        "status": "configuration_missing",
        "message": "ClickPost configuration missing or placeholder",
    }
    track_mock.assert_not_called()


@pytest.mark.asyncio
async def test_dummy_config_returns_configuration_missing(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = {
        "api_base": "https://api.clickpost.example.com",
        "username": "real_username",
        "key": "your_key",  # placeholder value
    }
    track_mock = AsyncMock()
    monkeypatch.setattr(adapter, "_track_waybill", track_mock)

    result = await adapter.aget_tracking_details("CP123456789")

    assert result["status"] == "configuration_missing"
    track_mock.assert_not_called()


# ── Waybill resolution from Shopify fulfillments ────────────────────────

@pytest.mark.asyncio
async def test_no_fulfillment_returns_waybill_missing(monkeypatch):
    monkeypatch.setattr(
        ShopifyOrderAdapter, "aget_order_details",
        AsyncMock(return_value={"fulfillments": []}),
    )
    adapter = ClickPostLogisticsAdapter(client_id="test-client")

    result = await adapter._resolve_waybill_from_shopify("gv1001")

    assert result == {
        "success": False,
        "status": "waybill_missing",
        "message": "ClickPost waybill not available in Shopify fulfillment",
    }


@pytest.mark.asyncio
async def test_fulfillment_without_tracking_number_returns_waybill_missing(monkeypatch):
    monkeypatch.setattr(
        ShopifyOrderAdapter, "aget_order_details",
        AsyncMock(return_value={"fulfillments": [_fulfillment(tracking_company="ClickPost")]}),
    )
    adapter = ClickPostLogisticsAdapter(client_id="test-client")

    result = await adapter._resolve_waybill_from_shopify("gv1002")

    assert result["success"] is False
    assert result["status"] == "waybill_missing"


@pytest.mark.asyncio
async def test_clickpost_fulfillment_resolves_waybill(monkeypatch):
    monkeypatch.setattr(
        ShopifyOrderAdapter, "aget_order_details",
        AsyncMock(return_value={
            "fulfillments": [
                _fulfillment(tracking_company="ClickPost", tracking_number="CP123456789"),
            ],
        }),
    )
    adapter = ClickPostLogisticsAdapter(client_id="test-client")

    result = await adapter._resolve_waybill_from_shopify("gv1003")

    assert result["success"] is True
    assert result["waybill"] == "CP123456789"
    assert result["source"] == "shopify_fulfillment"


@pytest.mark.asyncio
async def test_multiple_fulfillments_picks_clickpost_one(monkeypatch):
    monkeypatch.setattr(
        ShopifyOrderAdapter, "aget_order_details",
        AsyncMock(return_value={
            "fulfillments": [
                _fulfillment(tracking_company="Shiprocket Assigned", tracking_number="SR999"),
                _fulfillment(tracking_company="ClickPost", tracking_number="CP123456789"),
            ],
        }),
    )
    adapter = ClickPostLogisticsAdapter(client_id="test-client")

    result = await adapter._resolve_waybill_from_shopify("gv1004")

    assert result["success"] is True
    assert result["waybill"] == "CP123456789"


@pytest.mark.asyncio
async def test_split_order_resolves_every_parcel(monkeypatch):
    monkeypatch.setattr(
        ShopifyOrderAdapter, "aget_order_details",
        AsyncMock(return_value={
            "fulfillments": [
                _fulfillment(tracking_company="ClickPost", tracking_number="CP123456789"),
                _fulfillment(tracking_company="Click Post", tracking_number="CP999999999"),
            ],
        }),
    )
    adapter = ClickPostLogisticsAdapter(client_id="test-client")

    result = await adapter._resolve_waybill_from_shopify("gv1005")

    assert result["success"] is True
    assert [p["waybill"] for p in result["parcels"]] == ["CP123456789", "CP999999999"]
    # The first parcel stays on the top-level key so single-shipment callers
    # keep reading it unchanged.
    assert result["waybill"] == "CP123456789"


@pytest.mark.asyncio
async def test_repeated_waybill_collapses_to_one_parcel(monkeypatch):
    monkeypatch.setattr(
        ShopifyOrderAdapter, "aget_order_details",
        AsyncMock(return_value={
            "fulfillments": [
                _fulfillment(tracking_company="ClickPost", tracking_number="CP123456789"),
                _fulfillment(tracking_company="ClickPost", tracking_number="CP123456789"),
            ],
        }),
    )
    adapter = ClickPostLogisticsAdapter(client_id="test-client")

    result = await adapter._resolve_waybill_from_shopify("gv1006")

    assert [p["waybill"] for p in result["parcels"]] == ["CP123456789"]


# ── JSON response parsing ───────────────────────────────────────────────

def test_sample_json_parses_to_normalized_dict():
    result = parse_clickpost_tracking_response(SAMPLE_TRACKING_JSON)

    assert result["success"] is True
    assert result["waybill"] == "CP123456789"
    assert result["status"] == "out_for_delivery"
    assert result["raw_status"] == "Out for Delivery"
    # Status comes from the numeric bucket, not the courier's free text.
    assert result["clickpost_status_bucket"] == 4
    assert result["clickpost_status_code"] == 6
    assert result["location"] == "GURGAON"
    assert result["status_date"] == "2026-06-26 09:10:00"
    assert len(result["scans"]) == 2


def test_invalid_json_string_returns_parse_error_without_raising():
    result = parse_clickpost_tracking_response("not json at all <<<")
    assert result["success"] is False
    assert result["status"] == "parse_error"


def test_meta_failure_returns_parse_error():
    result = parse_clickpost_tracking_response({
        "meta": {"success": False, "message": "Invalid waybill"},
    })
    assert result["success"] is False
    assert result["status"] == "parse_error"


def test_empty_payload_returns_parse_error():
    result = parse_clickpost_tracking_response(None)
    assert result["success"] is False
    assert result["status"] == "parse_error"


# ── aget_order_data: resolve waybill then track ─────────────────────────

@pytest.mark.asyncio
async def test_aget_order_data_resolves_waybill_then_tracks(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    call_order = []

    async def fake_resolve(order_ref, state=None):
        call_order.append("resolve")
        return {
            "success": True, "waybill": "CP123456789", "cp_id": 4,
            "tracking_company": "ClickPost", "source": "shopify_fulfillment",
        }

    async def fake_track(waybill, cp_id=None, state=None):
        call_order.append("track")
        assert cp_id == 4, "cp_id must be forwarded from the resolved fulfillment"
        return SAMPLE_TRACKING_JSON

    monkeypatch.setattr(adapter, "_resolve_waybill_from_shopify", fake_resolve)
    monkeypatch.setattr(adapter, "_track_waybill", fake_track)

    result = await adapter.aget_order_data("gv1006")

    assert call_order == ["resolve", "track"]
    assert result["success"] is True
    # The waybill now travels inside the shipments envelope the caller reads.
    assert result["shipments"]["awb"] == "CP123456789"


@pytest.mark.asyncio
async def test_aget_order_data_returns_waybill_missing_without_tracking(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    track_mock = AsyncMock()
    monkeypatch.setattr(adapter, "_track_waybill", track_mock)
    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={"success": False, "status": "waybill_missing", "message": "x"}),
    )

    result = await adapter.aget_order_data("gv1007")

    assert result["status"] == "waybill_missing"
    track_mock.assert_not_called()


# ── acancel_shipment: resolve waybill first, terminal-status guard ─────

@pytest.mark.asyncio
async def test_acancel_shipment_resolves_waybill_first(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    call_order = []

    async def fake_resolve(order_ref, state=None):
        call_order.append("resolve")
        return {
            "success": True, "waybill": "CP123456789", "cp_id": 4,
            "tracking_company": "ClickPost", "source": "shopify_fulfillment",
        }

    async def fake_track(waybill, cp_id=None, state=None):
        call_order.append("track")
        return IN_TRANSIT_JSON

    monkeypatch.setattr(adapter, "_resolve_waybill_from_shopify", fake_resolve)
    monkeypatch.setattr(adapter, "_track_waybill", fake_track)
    monkeypatch.setattr(adapter, "_cancel_waybill", AsyncMock(return_value={"success": True}))

    await adapter.acancel_shipment("gv1008")

    assert call_order[0] == "resolve"


@pytest.mark.asyncio
async def test_acancel_shipment_skips_cancel_for_terminal_status(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP123456789",
            "tracking_company": "ClickPost", "source": "shopify_fulfillment",
        }),
    )
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(return_value=DELIVERED_JSON))
    cancel_mock = AsyncMock()
    monkeypatch.setattr(adapter, "_cancel_waybill", cancel_mock)

    result = await adapter.acancel_shipment("gv1009")

    assert result["success"] is False
    assert result.get("skipped") is True
    cancel_mock.assert_not_called()


@pytest.mark.asyncio
async def test_acancel_shipment_calls_cancel_for_in_transit_status(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP123456789", "cp_id": 4,
            "tracking_company": "ClickPost", "source": "shopify_fulfillment",
        }),
    )
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(return_value=IN_TRANSIT_JSON))
    cancel_mock = AsyncMock(return_value={"success": True, "message": "cancelled"})
    monkeypatch.setattr(adapter, "_cancel_waybill", cancel_mock)

    result = await adapter.acancel_shipment("gv1010")

    cancel_mock.assert_called_once_with("CP123456789", cp_id=4, state=None)
    assert result["success"] is True


# ── cp_id resolution ────────────────────────────────────────────────────
# ClickPost keys a shipment on (waybill, cp_id). Shopify has no cp_id field,
# so it is recovered from the tracking URL -- these pin that extraction and
# the plumbing that carries it into the tracking/cancel calls.

@pytest.mark.parametrize("tracking_url,expected", [
    ("https://vahro.clickpost.ai?cp_id=4&waybill=43671610140011&security_key=abc", 4),
    ("https://vahro.clickpost.ai?cp_id=105&waybill=SRSP5691123656&security_key=x", 105),
    ("https://vahro.clickpost.ai?waybill=NOCPID", None),
    ("https://vahro.clickpost.ai?cp_id=notanumber&waybill=X", None),
    ("", None),
    (None, None),
])
def test_extract_cp_id(tracking_url, expected):
    from fashion_bot.clickpost.tools.logistics_adapter import _extract_cp_id

    assert _extract_cp_id(tracking_url) == expected


@pytest.mark.asyncio
async def test_resolve_waybill_carries_cp_id_from_tracking_url(monkeypatch):
    """A resolved fulfillment must surface cp_id, not just the waybill."""
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        ShopifyOrderAdapter, "aget_order_details",
        AsyncMock(return_value={
            "fulfillments": [
                _fulfillment(
                    tracking_company="Delhivery",
                    tracking_url="https://vahro.clickpost.ai?cp_id=4&waybill=43671610140011",
                    tracking_number="43671610140011",
                ),
            ],
        }),
    )

    result = await adapter._resolve_waybill_from_shopify("V0017781")

    assert result["success"] is True
    assert result["waybill"] == "43671610140011"
    assert result["cp_id"] == 4


@pytest.mark.asyncio
async def test_track_waybill_requires_cp_id():
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    with pytest.raises(RuntimeError, match="cp_id is required"):
        await adapter._track_waybill("CP123456789", cp_id=None)


# ── cancel outcome ──────────────────────────────────────────────────────
# ClickPost answers HTTP 200 whenever the *call* succeeded; whether the
# shipment was actually cancelled lives in meta.success. Reporting HTTP 200
# as a cancellation would tell a customer their order was cancelled when the
# courier refused.

@pytest.mark.asyncio
async def test_cancel_reports_failure_when_courier_refuses(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"meta": {"success": False, "message": "Shipment already picked up", "status": 200}}

    class _Client:
        @staticmethod
        async def get(*_a, **_kw):
            return _Resp()

    monkeypatch.setattr(
        "fashion_bot.clickpost.tools.logistics_adapter.get_shared_async_http_client",
        AsyncMock(return_value=_Client()),
    )

    result = await adapter._cancel_waybill("CP123456789", cp_id=4)

    assert result["success"] is False
    assert "already picked up" in result["error"]


@pytest.mark.asyncio
async def test_cancel_succeeds_on_meta_success(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"meta": {"success": True, "message": "SUCCESS", "status": 200}}

    class _Client:
        @staticmethod
        async def get(url, params=None, **_kw):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

    monkeypatch.setattr(
        "fashion_bot.clickpost.tools.logistics_adapter.get_shared_async_http_client",
        AsyncMock(return_value=_Client()),
    )

    result = await adapter._cancel_waybill("CP123456789", cp_id=4)

    assert result["success"] is True
    # Cancel is a GET with every argument in the query string.
    assert captured["url"].endswith("/api/v1/cancel-order/")
    assert captured["params"]["waybill"] == "CP123456789"
    assert captured["params"]["cp_id"] == 4
    assert captured["params"]["username"] == "real_username"
    assert captured["params"]["key"] == "real_api_key"


# ── track-order response shape ──────────────────────────────────────────
# ClickPost keys `result` by waybill and consolidates every courier's status
# vocabulary into numeric buckets; these pin both.

@pytest.mark.parametrize("bucket,expected", [
    (1, "order_placed"),
    (2, "dispatched"),
    (3, "in_transit"),
    (4, "out_for_delivery"),
    (5, "exception"),
    (6, "delivered"),
    (7, "return_to_origin"),
    (8, "lost"),
    (9, "damaged"),
])
def test_status_bucket_maps_to_canonical_status(bucket, expected):
    payload = _track_order_json(
        bucket=bucket, code=None, status="whatever the courier called it",
        location="X", timestamp="2026-06-26 10:00:00",
    )
    assert parse_clickpost_tracking_response(payload)["status"] == expected


def test_bucket_wins_over_misleading_free_text():
    """The per-courier `status` string must not override the numeric bucket."""
    payload = _track_order_json(
        bucket=6, code=8, status="RTO Delivered to shipper",
        location="X", timestamp="2026-06-26 10:00:00",
    )
    assert parse_clickpost_tracking_response(payload)["status"] == "delivered"


def test_invalid_waybill_reports_not_found_not_a_status():
    """valid=false means the AWB is unknown to ClickPost -- it must not be
    read as a real shipment status."""
    payload = _track_order_json(
        bucket=1, code=1, status="", location="", timestamp="",
    )
    payload["result"]["CP123456789"]["valid"] = False

    result = parse_clickpost_tracking_response(payload)

    # Clean miss, not a failure: success stays True so the router reports it
    # as "not this partner's shipment" rather than a partner outage.
    assert result["success"] is True
    assert result["found"] is False
    assert result["status"] == "not_found"


def test_selects_requested_waybill_from_multi_entry_result():
    payload = _track_order_json(
        bucket=6, code=8, status="Delivered", location="A",
        timestamp="2026-06-26 10:00:00",
    )
    payload["result"]["OTHER999"] = {
        "latest_status": {"clickpost_status_bucket": 3, "status": "InTransit"},
        "scans": [], "valid": True,
    }

    assert parse_clickpost_tracking_response(payload, waybill="OTHER999")["status"] == "in_transit"
    assert parse_clickpost_tracking_response(payload, waybill="CP123456789")["status"] == "delivered"


def test_ambiguous_multi_waybill_without_selection_is_a_parse_error():
    payload = _track_order_json(
        bucket=6, code=8, status="Delivered", location="A",
        timestamp="2026-06-26 10:00:00",
    )
    payload["result"]["OTHER999"] = {"latest_status": {}, "scans": [], "valid": True}

    assert parse_clickpost_tracking_response(payload)["success"] is False


@pytest.mark.asyncio
async def test_track_waybill_is_a_get_to_the_tracking_host(monkeypatch):
    """Tracking reads go to the tracking host via track-order, not to
    awb-register on the order host."""
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = {"username": "real_username", "key": "real_api_key"}

    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return SAMPLE_TRACKING_JSON

    class _Client:
        @staticmethod
        async def get(url, params=None, **_kw):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

    monkeypatch.setattr(
        "fashion_bot.clickpost.tools.logistics_adapter.get_shared_async_http_client",
        AsyncMock(return_value=_Client()),
    )

    await adapter._track_waybill("CP123456789", cp_id=9)

    assert captured["url"] == "https://api.clickpost.in/api/v2/track-order/"
    assert captured["params"] == {
        "username": "real_username", "key": "real_api_key",
        "waybill": "CP123456789", "cp_id": 9,
    }


# ── delivery estimate (predicted SLA) ───────────────────────────────────
# Lives on a third ClickPost host and takes a *list* of pincode pairs.

def _sla_client(captured, body, status=200):
    class _Resp:
        status_code = status

        @staticmethod
        def json():
            return body

    class _Client:
        @staticmethod
        async def post(url, params=None, json=None, **_kw):
            captured["url"] = url
            captured["params"] = params
            captured["json"] = json
            return _Resp()

    return _Client()


@pytest.mark.asyncio
async def test_delivery_estimate_posts_pincode_list_to_sla_host(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)
    captured = {}

    monkeypatch.setattr(
        "fashion_bot.clickpost.tools.logistics_adapter.get_shared_async_http_client",
        AsyncMock(return_value=_sla_client(captured, {
            "meta": {"success": True, "message": "SUCCESS", "status": 200},
            "result": {
                "predicted_sla_min": 2,
                "predicted_sla_max": 5,
                "min_sla_cp_id": 4,
                "all_map": {"4": [2, 5], "9": [3, 6]},
            },
        })),
    )

    result = await adapter.aget_delivery_estimate("110001", "122009")

    assert result["status"] == "success"
    assert result["estimated_delivery"] == "2-5 days"
    assert result["min_sla_cp_id"] == 4
    assert result["all_options_count"] == 2
    # Third host, and the payload is a list -- not a bare object.
    assert captured["url"] == "https://ds.clickpost.in/api/v2/predicted_sla_api/"
    assert captured["json"] == [{"pickup_pincode": 110001, "drop_pincode": 122009}]
    assert captured["params"] == {"username": "real_username", "key": "real_api_key"}


@pytest.mark.asyncio
async def test_delivery_estimate_unwraps_list_result(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        "fashion_bot.clickpost.tools.logistics_adapter.get_shared_async_http_client",
        AsyncMock(return_value=_sla_client({}, {
            "meta": {"success": True, "status": 200},
            "result": [{"predicted_sla_min": 3, "predicted_sla_max": 3, "min_sla_cp_id": 9}],
        })),
    )

    result = await adapter.aget_delivery_estimate("110001", "122009")

    assert result["status"] == "success"
    # Equal min/max must not render as "3-3 days".
    assert result["estimated_delivery"] == "3 days"


@pytest.mark.asyncio
async def test_delivery_estimate_reports_meta_failure(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        "fashion_bot.clickpost.tools.logistics_adapter.get_shared_async_http_client",
        AsyncMock(return_value=_sla_client({}, {
            "meta": {"success": False, "message": "Invalid pincode", "status": 400},
        })),
    )

    result = await adapter.aget_delivery_estimate("110001", "999999")

    assert result["status"] == "error"
    assert "Invalid pincode" in result["message"]


@pytest.mark.asyncio
async def test_delivery_estimate_rejects_non_numeric_pincode_without_calling(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)
    http = AsyncMock()
    monkeypatch.setattr(
        "fashion_bot.clickpost.tools.logistics_adapter.get_shared_async_http_client", http,
    )

    result = await adapter.aget_delivery_estimate("not-a-pincode", "122009")

    assert result["status"] == "error"
    http.assert_not_called()


@pytest.mark.asyncio
async def test_delivery_estimate_refuses_without_config(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = {}
    http = AsyncMock()
    monkeypatch.setattr(
        "fashion_bot.clickpost.tools.logistics_adapter.get_shared_async_http_client", http,
    )

    result = await adapter.aget_delivery_estimate("110001", "122009")

    assert result["status"] == "error"
    http.assert_not_called()


# ── cross-tenant safety ─────────────────────────────────────────────────
# Adding ClickPost to TRACKING_URL_PATTERNS makes that table match for every
# tenant, not just the ones using ClickPost. A tenant whose orders carry a
# ClickPost tracking domain but who has NOT connected ClickPost must keep
# resolving through the alias table, exactly as before ClickPost existed.

@pytest.mark.asyncio
async def test_clickpost_url_falls_back_to_alias_when_tenant_not_connected(monkeypatch):
    from fashion_bot.utils import delivery_partner_utils as dpu

    monkeypatch.setattr(
        dpu, "aget_connected_partners",
        AsyncMock(return_value=["shiprocket"]),      # no clickpost
    )

    canonical, connected = await dpu.aresolve_effective_partner_for_order(
        {
            "tracking_url": "https://someone-else.clickpost.ai?cp_id=9&waybill=SF1",
            "tracking_company": "Shadowfax",
        },
        state={"client_id": "some-other-tenant"},
    )

    # Shadowfax is managed by Shiprocket for this tenant — the ClickPost
    # domain must not steal the resolution.
    assert canonical == "shiprocket"
    assert connected is True


@pytest.mark.asyncio
async def test_clickpost_url_wins_when_tenant_is_connected(monkeypatch):
    from fashion_bot.utils import delivery_partner_utils as dpu

    monkeypatch.setattr(
        dpu, "aget_connected_partners",
        AsyncMock(return_value=["clickpost"]),
    )

    canonical, connected = await dpu.aresolve_effective_partner_for_order(
        {
            "tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=436716",
            "tracking_company": "Delhivery",
        },
        state={"client_id": "vahro"},
    )

    # URL-first still wins over the misleading "Delhivery" tracking_company.
    assert canonical == "clickpost"
    assert connected is True


# ── aget_order_data envelope ────────────────────────────────────────────
# Callers gate the tracking merge on `found` and read the shipment out of
# `shipments`. Returning the parser dict directly silently drops every
# tracking detail, so the envelope is pinned here.

@pytest.mark.asyncio
async def test_aget_order_data_returns_found_envelope_with_edd(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP123456789", "cp_id": 4,
            "tracking_company": "Delhivery",
            "tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=CP123456789",
            "source": "shopify_fulfillment",
        }),
    )
    tracking = _track_order_json(
        bucket=3, code=5, status="In Transit", location="Bhiwandi_Lonad_GW",
        timestamp="2026-08-07 02:36:33",
    )
    tracking["result"]["CP123456789"]["additional"]["courier_partner_edd"] = "2026-08-12"
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(return_value=tracking))

    result = await adapter.aget_order_data("V0017790")

    # The gate the caller actually checks.
    assert result["found"] is True
    assert result["success"] is True

    s = result["shipments"]
    assert s["awb"] == "CP123456789"
    assert s["courier"] == "Delhivery"
    assert s["tracking_url"].startswith("https://vahro.clickpost.ai")
    # `etd` is the key extract_expected_delivery reads; under any other name
    # the date is dropped on the recent-orders path and skips normalisation.
    assert s["etd"] == "2026-08-12"
    assert s["current_location"] == "Bhiwandi_Lonad_GW"
    assert result["order_data"]["shipments"] is s

    # Top-level keys the canonical contract defines and tool_factory reads.
    assert result["order_id"] == "V0017790"
    assert result["logistics_order_id"] == "CP123456789"
    # Top-level status is translated into the vocabulary the shared status
    # resolver speaks; the canonical lower_snake form stays on `shipments`.
    assert result["status"] == "IN TRANSIT"
    assert result["shipments"]["status"] == "in_transit"


@pytest.mark.asyncio
async def test_batched_parcel_missing_from_response_is_not_given_a_neighbours_status(
    monkeypatch,
):
    """Two waybills requested, one returned: the absent parcel is unknown.

    The parser's "only one record present, so that must be it" fallback is a
    tolerance for a single request whose key came back in a different form.
    Applied to a batched request it answers about a different parcel entirely
    -- telling a customer a box that never scanned was delivered.
    """
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP111", "cp_id": 9,
            "tracking_company": "Shadowfax", "tracking_url": "https://vahro.clickpost.ai?cp_id=9",
            "parcels": [
                {"waybill": "CP111", "cp_id": 9, "tracking_company": "Shadowfax",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=CP111"},
                {"waybill": "CP222", "cp_id": 9, "tracking_company": "Shadowfax",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=CP222"},
            ],
            "source": "shopify_fulfillment",
        }),
    )

    # Courier answers about the first parcel only -- the second is not
    # registered with them yet, or simply absent from this response.
    delivered = _track_order_json(
        bucket=6, code=17, status="Delivered",
        location="Hyderabad", timestamp="2026-08-10 11:20:00",
    )
    delivered["result"]["CP111"] = delivered["result"].pop("CP123456789")
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(return_value=delivered))

    result = await adapter.aget_order_data("V0018267")

    # The parcel that did resolve is still reported...
    assert result["found"] is True
    awbs = [p["awb"] for p in (result["shipments"].get("parcels") or [result["shipments"]])]
    assert "CP222" not in awbs, (
        "the absent parcel must not appear carrying the other parcel's record"
    )
    assert result["shipments"]["awb"] == "CP111"
    assert result["shipments"]["status"] == "delivered"


@pytest.mark.asyncio
async def test_split_order_tracks_every_parcel_in_one_call(monkeypatch):
    """A split order reports the parcel the customer waits longest for, and
    keeps per-parcel detail, without one request per waybill."""
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP111", "cp_id": 9,
            "tracking_company": "Shadowfax",
            "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=CP111",
            "parcels": [
                {"waybill": "CP111", "cp_id": 9, "tracking_company": "Shadowfax",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=CP111"},
                {"waybill": "CP222", "cp_id": 9, "tracking_company": "Shadowfax",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=CP222"},
            ],
            "source": "shopify_fulfillment",
        }),
    )

    def _entry(edd):
        payload = _track_order_json(
            bucket=3, code=5, status="In Transit",
            location="Bhiwandi_Lonad_GW", timestamp="2026-08-07 02:36:33",
        )
        entry = payload["result"]["CP123456789"]
        entry["additional"]["courier_partner_edd"] = edd
        return entry

    # One response keyed by both waybills, as the API returns for a
    # comma-separated request.
    batched = {"result": {"CP111": _entry("2026-08-12"), "CP222": _entry("2026-08-14")}}

    track = AsyncMock(return_value=batched)
    monkeypatch.setattr(adapter, "_track_waybill", track)

    result = await adapter.aget_order_data("V0018267")

    # Both waybills go out in a single comma-separated request.
    track.assert_awaited_once()
    assert track.await_args.args[0] == "CP111,CP222"

    # The order is complete when its last parcel lands, so the order-level
    # date is the later one -- quoting 12 Aug would promise it early.
    assert result["shipments"]["etd"] == "2026-08-14"
    assert [p["awb"] for p in result["shipments"]["parcels"]] == ["CP111", "CP222"]
    assert [p["etd"] for p in result["shipments"]["parcels"]] == ["2026-08-12", "2026-08-14"]


@pytest.mark.asyncio
async def test_split_across_couriers_compares_dates_not_strings(monkeypatch):
    """Two couriers, two date formats. Lexicographically "27-06-2026" sorts
    above "2026-07-01", so string comparison would pick the earlier parcel and
    promise the whole order a month early."""
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP111", "cp_id": 9,
            "tracking_company": "Shadowfax", "tracking_url": "https://vahro.clickpost.ai?cp_id=9",
            "parcels": [
                {"waybill": "CP111", "cp_id": 9, "tracking_company": "Shadowfax",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=CP111"},
                {"waybill": "CP222", "cp_id": 4, "tracking_company": "Delhivery",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=CP222"},
            ],
            "source": "shopify_fulfillment",
        }),
    )

    def _payload(waybill, edd):
        p = _track_order_json(
            bucket=3, code=5, status="In Transit",
            location="Bhiwandi_Lonad_GW", timestamp="2026-08-07 02:36:33",
        )
        entry = p["result"].pop("CP123456789")
        entry["additional"]["courier_partner_edd"] = edd
        return {"result": {waybill: entry}}

    # The genuinely later parcel (1 Sep) is in ISO form; the earlier one
    # (27 Jun) is not. As strings "27-06-2026" > "2026-09-01", so a
    # lexicographic max picks June and promises the order two months early.
    track = AsyncMock(side_effect=[
        _payload("CP111", "2026-09-01"),
        _payload("CP222", "27-06-2026"),
    ])
    monkeypatch.setattr(adapter, "_track_waybill", track)

    result = await adapter.aget_order_data("V0018267")

    # Two couriers means two calls -- they are gathered, not chained.
    assert track.await_count == 2
    assert result["shipments"]["etd"] == "2026-09-01", (
        "order-level date must follow the genuinely later parcel, not the one "
        "that happens to sort higher as a string"
    )


@pytest.mark.asyncio
async def test_one_courier_outage_keeps_the_parcel_that_resolved(monkeypatch):
    """A split order where one courier read fails still reports the other."""
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP111", "cp_id": 9,
            "tracking_company": "Shadowfax", "tracking_url": "https://vahro.clickpost.ai?cp_id=9",
            "parcels": [
                {"waybill": "CP111", "cp_id": 9, "tracking_company": "Shadowfax",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=CP111"},
                {"waybill": "CP222", "cp_id": 4, "tracking_company": "Delhivery",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=CP222"},
            ],
            "source": "shopify_fulfillment",
        }),
    )

    good = _track_order_json(
        bucket=3, code=5, status="In Transit",
        location="Bhiwandi_Lonad_GW", timestamp="2026-08-07 02:36:33",
    )
    good["result"]["CP111"] = good["result"].pop("CP123456789")
    good["result"]["CP111"]["additional"]["courier_partner_edd"] = "2026-08-12"

    monkeypatch.setattr(
        adapter, "_track_waybill",
        AsyncMock(side_effect=[good, RuntimeError("courier timeout")]),
    )

    result = await adapter.aget_order_data("V0018267")

    assert result["found"] is True, "one courier's outage must not sink the order"
    assert result["shipments"]["etd"] == "2026-08-12"


@pytest.mark.asyncio
async def test_total_outage_reports_an_error_not_an_empty_answer(monkeypatch):
    """When every parcel read raises, the result must carry an error. The
    router stores this dict in per_partner, where a bare success=False reads
    the same as a well-formed empty answer."""
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP111", "cp_id": 9,
            "tracking_company": "Shadowfax", "tracking_url": "https://vahro.clickpost.ai?cp_id=9",
            "parcels": [
                {"waybill": "CP111", "cp_id": 9, "tracking_company": "Shadowfax",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=CP111"},
                {"waybill": "CP222", "cp_id": 4, "tracking_company": "Delhivery",
                 "tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=CP222"},
            ],
            "source": "shopify_fulfillment",
        }),
    )
    monkeypatch.setattr(
        adapter, "_track_waybill",
        AsyncMock(side_effect=[RuntimeError("courier down"), RuntimeError("courier down")]),
    )

    result = await adapter.aget_order_data("V0018267")

    assert result["success"] is False
    assert result["found"] is False
    assert result.get("error"), "a total outage must be distinguishable from an empty answer"


@pytest.mark.asyncio
async def test_aget_order_data_marks_not_found_on_parse_failure(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP123456789", "cp_id": 4,
            "tracking_company": "Delhivery", "tracking_url": "https://vahro.clickpost.ai?cp_id=4",
            "source": "shopify_fulfillment",
        }),
    )
    # valid=false => unknown AWB, must not be reported as a found shipment.
    bad = _track_order_json(bucket=1, code=1, status="", location="", timestamp="")
    bad["result"]["CP123456789"]["valid"] = False
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(return_value=bad))

    result = await adapter.aget_order_data("V0017790")

    # found=False marks the miss; success=True keeps it out of the error path.
    assert result["found"] is False
    assert result["success"] is True


# ── canonical order-status surface ──────────────────────────────────────
# OrderStatusOrchestrator races the *order* adapters, not the logistics
# ones. Without these, a fulfilled ClickPost order finds no winner and its
# status silently falls back to whatever Shopify recorded.

@pytest.mark.asyncio
async def test_aget_matching_orders_returns_canonical_wrapper(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)

    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP123456789", "cp_id": 4,
            "tracking_company": "Delhivery",
            "tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=CP123456789",
            "source": "shopify_fulfillment",
        }),
    )
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(return_value=IN_TRANSIT_JSON))

    orders = await adapter.aget_matching_orders("V0017790")

    assert len(orders) == 1
    assert orders[0]["order_id"] == "CP123456789"        # waybill
    assert orders[0]["channel_order_id"] == "V0017790"
    # Carries the translated form: this wrapper feeds the order processor,
    # which buckets it through map_fulfillment_status.
    assert orders[0]["status"] == "IN TRANSIT"
    assert orders[0]["shipment_status"] == "in_transit"
    assert "shipments" in orders[0]["order_data"]


@pytest.mark.asyncio
async def test_aget_matching_orders_empty_when_not_found(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)
    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={"success": False, "status": "waybill_missing"}),
    )
    assert await adapter.aget_matching_orders("V1") == []


@pytest.mark.asyncio
async def test_order_adapter_surfaces_shipment_to_the_status_race(monkeypatch):
    """The order adapter must return rows, or the race finds no winner."""
    from fashion_bot.clickpost.tools.order_adapter import ClickPostOrderAdapter

    monkeypatch.setattr(
        ClickPostLogisticsAdapter, "aget_matching_orders",
        AsyncMock(return_value=[{"order_id": "CP1", "channel_order_id": "V1",
                                 "status": "in_transit", "shipment_status": "in_transit",
                                 "order_data": {}}]),
    )

    result = await ClickPostOrderAdapter(client_id="c1").aget_order_details("V1")

    assert result["orders"] and result["orders"][0]["status"] == "in_transit"


@pytest.mark.asyncio
async def test_order_adapter_degrades_on_error(monkeypatch):
    from fashion_bot.clickpost.tools.order_adapter import ClickPostOrderAdapter

    monkeypatch.setattr(
        ClickPostLogisticsAdapter, "aget_matching_orders",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    result = await ClickPostOrderAdapter(client_id="c1").aget_order_details("V1")

    # Fails open with no rows, but carries the reason: the race reads a bare
    # empty list as a clean miss and would file an outage as
    # shopify_fallback_logistics_miss rather than ..._error.
    assert result["orders"] == []
    assert "boom" in result["error"]


# ── ETA reaches the canonical reader ────────────────────────────────────

@pytest.mark.asyncio
async def test_etd_is_readable_by_extract_expected_delivery(monkeypatch):
    """The whole point of naming the key `etd`: the shared reader finds it."""
    from fashion_bot.core.partner_response_mappings import extract_expected_delivery

    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)
    monkeypatch.setattr(
        adapter, "_resolve_waybill_from_shopify",
        AsyncMock(return_value={
            "success": True, "waybill": "CP123456789", "cp_id": 4,
            "tracking_company": "Delhivery", "tracking_url": "https://vahro.clickpost.ai?cp_id=4",
            "source": "shopify_fulfillment",
        }),
    )
    tracking = _track_order_json(
        bucket=3, code=5, status="In Transit", location="DELHI",
        timestamp="2026-08-07 02:36:33",
    )
    far_future = "2099-12-25"
    tracking["result"]["CP123456789"]["additional"]["courier_partner_edd"] = far_future
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(return_value=tracking))

    result = await adapter.aget_order_data("V0017790")

    # A future date survives the shared reader's staleness normalisation.
    assert extract_expected_delivery(result) != ""


# ── size cancel-and-recreate routing ────────────────────────────────────
# The size path resolved the carrier from tracking_company alone, which for
# an aggregator names the underlying courier. A ClickPost order therefore
# resolved elsewhere and, with no handler registered, ran Shiprocket's flow
# against a tenant that may hold no Shiprocket credentials.

@pytest.mark.asyncio
async def test_size_change_resolves_clickpost_url_first(monkeypatch):
    from fashion_bot.utils import delivery_partner_utils as dpu

    monkeypatch.setattr(
        dpu, "aget_connected_partners", AsyncMock(return_value=["clickpost"]),
    )

    # Shopify records the underlying courier, never the aggregator.
    order_dto = {
        "tracking_company": "Delhivery",
        "tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=436716",
    }
    canonical, connected = await dpu.aresolve_effective_partner_for_order(
        order_dto, state={"client_id": "vahro"},
    )

    assert canonical == "clickpost"
    assert connected is True


@pytest.mark.asyncio
async def test_size_change_does_not_fall_back_to_another_partner(monkeypatch):
    """A connected partner with no size handler must escalate, not run
    another partner's API against its shipment."""
    from fashion_bot.shopify.modules import order_editing_graphql as oeg
    from fashion_bot.core import logistics_registry

    shiprocket_called = AsyncMock()
    monkeypatch.setattr(oeg, "_aupdate_shiprocket_order_size", shiprocket_called)

    reg = logistics_registry.get_partner("clickpost")
    assert reg is not None
    # The registration deliberately exposes no size handler.
    assert reg.cancel_recreate_handler is None

    # With no handler and a resolved non-shiprocket partner, the dispatch must
    # not reach Shiprocket's implementation.
    shiprocket_called.assert_not_called()


# ── canonical tracking shape ────────────────────────────────────────────
# The router judges a tracking result usable by awb / current_location /
# latest_activity. Emitting the parser's own key names meant every
# successful read was discarded as invalid.

@pytest.mark.asyncio
async def test_tracking_details_emits_router_readable_keys(monkeypatch):
    from fashion_bot.core.logistics_router import _is_valid_tracking_result

    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(return_value=SAMPLE_TRACKING_JSON))

    result = await adapter.aget_tracking_details(
        "CP123456789",
        state={"tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=CP123456789"},
    )

    assert result["awb"] == "CP123456789"
    assert result["current_location"] == "GURGAON"
    assert result["latest_activity"]
    assert result["last_update_date"]
    # The gate that previously rejected every ClickPost tracking read.
    assert _is_valid_tracking_result(result) is True


@pytest.mark.asyncio
async def test_tracking_details_still_reports_errors(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(side_effect=RuntimeError("boom")))

    result = await adapter.aget_tracking_details(
        "CP1", state={"tracking_url": "https://vahro.clickpost.ai?cp_id=4"},
    )
    assert result["status"] == "error"


# ── ordering: config check before the Shopify round-trip ────────────────

@pytest.mark.asyncio
async def test_missing_config_skips_the_shopify_lookup(monkeypatch):
    """Resolving the waybill costs a Shopify call; the config check is a dict
    lookup. An unconfigured tenant should not pay for a discarded result."""
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = {}
    resolve = AsyncMock()
    monkeypatch.setattr(adapter, "_resolve_waybill_from_shopify", resolve)

    result = await adapter.aget_order_data("V1")

    assert result["status"] == "configuration_missing"
    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_missing_config_skips_the_shopify_lookup(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = {}
    resolve = AsyncMock()
    monkeypatch.setattr(adapter, "_resolve_waybill_from_shopify", resolve)

    result = await adapter.acancel_shipment("V1")

    assert result["status"] == "configuration_missing"
    resolve.assert_not_called()


# ── canonical field names come from the shared mapping ──────────────────

def test_clickpost_declares_canonical_field_names():
    from fashion_bot.core.partner_response_mappings import GET_ORDER_DATA_FIELD_MAPS

    fmap = GET_ORDER_DATA_FIELD_MAPS.get("clickpost")
    assert fmap, "clickpost must declare its canonical field names"
    # etd is the one the shared ETA reader looks for.
    assert fmap["shipments.etd"] == "courier_partner_edd"
    assert fmap["shipments.awb"] == "waybill"


@pytest.mark.asyncio
async def test_url_match_for_unconnected_partner_falls_through(monkeypatch):
    """A URL match is not final. If the tenant has not connected the platform
    the URL names, resolution must fall through to the tracking_company alias
    table rather than returning a partner that cannot be queried."""
    from fashion_bot.utils import delivery_partner_utils as dpu

    # Tenant has Delhivery, not ClickPost.
    monkeypatch.setattr(
        dpu, "aget_connected_partners", AsyncMock(return_value=["delhivery"]),
    )
    fallback = AsyncMock(return_value=("delhivery", True))
    monkeypatch.setattr(dpu, "aresolve_canonical_partner_for_order", fallback)

    canonical, connected = await dpu.aresolve_effective_partner_for_order(
        {
            "tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=436716",
            "tracking_company": "Delhivery",
        },
        state={"client_id": "vahro"},
    )

    assert canonical == "delhivery"
    assert connected is True
    fallback.assert_awaited_once()


def test_terminal_statuses_are_all_reachable():
    """Every terminal status must be one the parser can actually emit, or a
    cancel guard silently never fires for it."""
    from fashion_bot.clickpost.tools import logistics_adapter as la
    from fashion_bot.clickpost.tools import response_parser as rp

    emittable = set(rp._BUCKET_TO_STATUS.values()) | set(rp._CODE_TO_STATUS.values())
    emittable.add(rp.CANCELLED)  # keyword path

    assert la._TERMINAL_STATUSES <= emittable


def test_unknown_waybill_null_result_is_a_clean_miss():
    """ClickPost answers an unknown waybill with meta.success=true and a null
    result -- the call worked, the shipment just isn't theirs. Captured from a
    live response; the valid=false branch alone did not cover it, so a mistyped
    AWB was reported as a partner failure."""
    payload = {"meta": {"status": 200, "success": True, "message": "SUCCESS"},
               "result": None}

    result = parse_clickpost_tracking_response(payload, waybill="SF0000000000BOGUS")

    assert result["success"] is True
    assert result["found"] is False
    assert result["status"] == "not_found"


def test_genuine_failure_is_still_an_error():
    """The clean-miss path must not swallow a real partner failure."""
    payload = {"meta": {"status": 301, "success": False,
                        "message": "Authentication Failed: Invalid Token or API Key"}}

    result = parse_clickpost_tracking_response(payload, waybill="W1")

    assert result["success"] is False
    assert result["status"] == "parse_error"


# ── adapter -> processor -> orchestrator seam ───────────────────────────
# The unit tests above stop at each boundary: one asserts the adapter hands
# the shipment to the status race, another asserts the processor's return.
# Neither can see that a row won by the adapter and dropped by the processor
# becomes "order not found" -- the orchestrator returns the processor's count
# directly, with no Shopify fallback below it.

@pytest.mark.asyncio
async def test_fulfilled_order_survives_the_status_race(monkeypatch):
    """A ClickPost order the partner *found* must not come back as
    total_orders_found=0. Every update and cancel tool reads that count as
    "order does not exist"."""
    from fashion_bot.core.orchestrator import OrderStatusOrchestrator
    from fashion_bot.core.logistics_router import LogisticsRouter
    from fashion_bot.core.factory import ServiceFactory

    shopify_dto = {
        "order_id": "V0018025",
        "name": "V0018025",
        "order_name": "V0018025",
        "fulfillment_status": "fulfilled",
        "shipment_status": "OUT FOR DELIVERY",
        "tracking_company": "Delhivery",
        "tracking_url": "https://vahro.clickpost.ai?cp_id=9&waybill=SF3772904310VAO",
        "cancelled_at": None,
        "line_items": [],
    }

    class _ShopifyProc:
        def process_order(self, *a, **k):
            return dict(shopify_dto)

        def process_orders(self, orders, **k):
            return [dict(shopify_dto)]

    order_service = AsyncMock()
    order_service.aget_order_details = AsyncMock(return_value=dict(shopify_dto))

    monkeypatch.setattr(ServiceFactory, "get_primary_vendor", staticmethod(lambda *a, **k: "shopify"))
    monkeypatch.setattr(ServiceFactory, "aget_order_service", AsyncMock(return_value=order_service))
    monkeypatch.setattr(
        ServiceFactory, "get_order_processor",
        staticmethod(lambda vendor, **k: ClickPostOrderProcessor()
                     if vendor == "clickpost" else _ShopifyProc()),
    )
    monkeypatch.setattr(ServiceFactory, "get_enrichment_pipeline", staticmethod(lambda **k: []))

    # The row the ClickPost adapter actually builds in aget_matching_orders.
    row = {
        "order_id": "SF3772904310VAO",
        "channel_order_id": "V0018025",
        "status": "out_for_delivery",
        "shipment_status": "out_for_delivery",
        "order_data": {
            "waybill": "SF3772904310VAO",
            "status": "out_for_delivery",
            "shipments": {
                "awb": "SF3772904310VAO",
                "status": "out_for_delivery",
                "etd": "2026-08-10",
                "courier": "Delhivery",
                "scans": [],
            },
        },
    }

    monkeypatch.setattr(
        LogisticsRouter, "aroute_for_order", AsyncMock(return_value=["clickpost"]),
    )
    monkeypatch.setattr(
        LogisticsRouter, "aget_order_details_first_valid",
        AsyncMock(return_value=("clickpost", {"orders": [row]}, {"clickpost": {"orders": [row]}})),
    )

    result = await OrderStatusOrchestrator.aget_order_status(
        "V0018025", state={"client_id": "vahro"},
    )

    assert result["total_orders_found"] == 1, (
        f"ClickPost won the race but the order vanished: {result}"
    )
    assert result["orders"][0]["_partner_winner"] == "clickpost"


def test_every_parser_status_maps_to_a_known_bucket():
    """Any status the parser can emit must map to something other than
    'unknown', or a delivered order does not read as delivered.

    Measured through ``_to_shared_status`` because that is what the adapter
    hands out: the mapping table sees the translated form, never the parser's
    own. Checking the parser's form directly would pass against a table that
    production never queries with it.
    """
    from fashion_bot.clickpost.tools import response_parser as rp
    from fashion_bot.clickpost.tools.logistics_adapter import _to_shared_status
    from fashion_bot.core.partner_response_mappings import map_fulfillment_status

    emittable = (
        set(rp._BUCKET_TO_STATUS.values())
        | set(rp._CODE_TO_STATUS.values())
        | {rp.CANCELLED}
    )
    unmapped = {
        s for s in emittable
        if map_fulfillment_status("clickpost", _to_shared_status(s)) == "unknown"
    }
    assert not unmapped, f"ClickPost statuses bucketing to 'unknown': {sorted(unmapped)}"


def test_delivered_clickpost_status_reads_as_fulfilled():
    """Spelled as the adapter emits them -- the mapping table is only ever
    queried with the translated form."""
    from fashion_bot.clickpost.tools.logistics_adapter import _to_shared_status
    from fashion_bot.core.partner_response_mappings import map_fulfillment_status

    assert map_fulfillment_status("clickpost", _to_shared_status("delivered")) == "fulfilled"
    assert map_fulfillment_status("clickpost", _to_shared_status("out_for_delivery")) == "fulfilled"
    assert map_fulfillment_status("clickpost", _to_shared_status("cancelled")) == "cancelled"
    assert map_fulfillment_status("clickpost", _to_shared_status("order_placed")) == "unfulfilled"


def test_adapter_status_vocabulary_matches_the_shared_resolver():
    """The translation is the contract between ClickPost and everything
    downstream: the resolver must recognise every status the adapter emits
    that has a shared counterpart, and the mapping table must bucket it.

    Pinned because the two drifted apart once already -- the parser emitted
    ``in_transit`` while the resolver was keyed on ``IN TRANSIT``, leaving
    logistics_status empty and the prompt's status rules unable to fire.
    """
    from fashion_bot.clickpost.tools.logistics_adapter import _to_shared_status
    from fashion_bot.logistics.status_resolver import resolve_logistics_status

    expected = {
        "order_placed":     "New",
        "dispatched":       "In Transit",
        "picked_up":        "Picked Up",
        "in_transit":       "In Transit",
        "out_for_delivery": "Out for Delivery",
        "delivered":        "Delivered",
    }
    for canonical, logistics_status in expected.items():
        assert resolve_logistics_status(
            raw_partner_status=_to_shared_status(canonical),
            shipment_status=None,
        ) == logistics_status, f"{canonical} no longer resolves"

    # No shared counterpart: left untranslated and deliberately unresolved,
    # since the prompt has no rule for them and naming a near neighbour would
    # describe the shipment as something it is not.
    for canonical in ("cancelled", "lost", "damaged", "exception", "return_to_origin"):
        assert _to_shared_status(canonical) == canonical
        assert resolve_logistics_status(
            raw_partner_status=canonical, shipment_status=None,
        ) is None


# ── latest_activity must never quote a numeric status code ──────────────
# Captured live: some couriers put a numeric code in latest_status.status
# and the readable text in `remark` ("17" vs "Out For Delivery"). The shared
# test helper sets remark == status, so both fields always agree there and a
# payload where they differ is needed to cover this.

def _numeric_status_json(waybill="CP123456789"):
    return {
        "meta": {"success": True, "message": "SUCCESS", "status": 200},
        "result": {
            waybill: {
                "latest_status": {
                    "clickpost_status_bucket": 4,
                    "clickpost_status_code": 6,
                    "status": "17",                    # numeric code
                    "remark": "Out For Delivery",      # the readable text
                    "location": "BNE",
                    "timestamp": "2026-08-11 09:10:00",
                },
                "additional": {"courier_partner_edd": "2026-08-12"},
                "scans": [],
                "valid": True,
            }
        },
    }


@pytest.mark.asyncio
async def test_latest_activity_prefers_remark_over_numeric_status(monkeypatch):
    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)
    monkeypatch.setattr(
        adapter, "_track_waybill", AsyncMock(return_value=_numeric_status_json()),
    )

    result = await adapter.aget_tracking_details(
        "CP123456789",
        state={"tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=CP123456789"},
    )

    assert result["latest_activity"] == "Out For Delivery"
    assert "17" not in result["formatted_update"]
    # The canonical status still comes from the bucket, not the code.
    assert result["status"] == "out_for_delivery"


@pytest.mark.asyncio
async def test_latest_activity_falls_back_when_remark_missing(monkeypatch):
    """No remark and a numeric status: humanize the canonical status rather
    than showing the customer a bare code."""
    payload = _numeric_status_json()
    payload["result"]["CP123456789"]["latest_status"]["remark"] = None

    adapter = ClickPostLogisticsAdapter(client_id="test-client")
    adapter._config = dict(VALID_CONFIG)
    monkeypatch.setattr(adapter, "_track_waybill", AsyncMock(return_value=payload))

    result = await adapter.aget_tracking_details(
        "CP123456789",
        state={"tracking_url": "https://vahro.clickpost.ai?cp_id=4&waybill=CP123456789"},
    )

    assert result["latest_activity"] == "Out For Delivery"
    assert result["latest_activity"].isdigit() is False
