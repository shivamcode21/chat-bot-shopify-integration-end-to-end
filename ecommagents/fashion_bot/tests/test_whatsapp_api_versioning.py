"""Tests for multi-version WhatsApp API support.

Covers:
  * version resolution (legacy default, enterprise selection, fail-open)
  * enterprise GatewayAPI query construction (template + session text)
  * URL-encode round-trip of the form body (spec §5)
  * tenant-scoped credential resolution
  * dispatch: the existing send functions delegate to the enterprise transport
    only when the tenant is configured for it, and otherwise leave the legacy
    path untouched
  * a live integration test (marked ``integration``, skipped by default) that
    sends a real template to 7503014404 using the gant client/secret from .env

Run the unit tests with::

    pytest tests/test_whatsapp_api_versioning.py

Run the live send (requires .env gant_client / gant_secret and a template id)::

    GANT_TEST_TEMPLATE_ID=<approved_id> \\
    pytest -m integration tests/test_whatsapp_api_versioning.py -k live -s
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlencode

import pytest

from fashion_bot.utils import whatsapp_api_version as wav
from fashion_bot.utils import whatsapp_enterprise_client as wec
from fashion_bot.utils.template_param_resolver import (
    build_template_params_from_context,
    resolve_template_params,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "response": {
                "id": "1234567890",
                "phone": "917503014404",
                "details": "Message sent successfully",
                "status": "success",
            }
        }
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeHTTPClient:
    """Captures requests so tests can assert on the GatewayAPI call."""

    def __init__(self, response=None):
        self.calls = []
        self._response = response or _FakeResponse()

    async def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({
            "method": "POST",
            "url": url,
            "data": data,
            "headers": headers,
            "timeout": timeout,
        })
        return self._response

    async def get(self, url, params=None, timeout=None):
        self.calls.append({
            "method": "GET",
            "url": url,
            "params": params,
            "timeout": timeout,
        })
        return self._response


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("enterprise", wav.WHATSAPP_API_VERSION_ENTERPRISE),
        ("ENTERPRISE", wav.WHATSAPP_API_VERSION_ENTERPRISE),
        ("v2", wav.WHATSAPP_API_VERSION_ENTERPRISE),
        (" gateway ", wav.WHATSAPP_API_VERSION_ENTERPRISE),
        ({"version": "enterprise"}, wav.WHATSAPP_API_VERSION_ENTERPRISE),
        ("legacy", wav.WHATSAPP_API_VERSION_LEGACY),
        ("v1", wav.WHATSAPP_API_VERSION_LEGACY),
        ("", wav.WHATSAPP_API_VERSION_LEGACY),
        (None, wav.WHATSAPP_API_VERSION_LEGACY),
        ({"unrelated": "x"}, wav.WHATSAPP_API_VERSION_LEGACY),
    ],
)
def test_normalize_whatsapp_api_version(raw, expected):
    assert wav.normalize_whatsapp_api_version(raw) == expected


async def test_version_defaults_legacy_without_client_id():
    assert await wav.aget_whatsapp_api_version(None) == wav.WHATSAPP_API_VERSION_LEGACY


async def test_version_enterprise_from_config(monkeypatch):
    async def _fake_aget_config(key, default=None, client_id=None):
        assert key == wav.WHATSAPP_API_VERSION_CONFIG_KEY
        assert client_id == "client-1"
        return "enterprise"

    monkeypatch.setattr("fashion_bot.config_manager.aget_config", _fake_aget_config)
    assert await wav.aget_whatsapp_api_version("client-1") == wav.WHATSAPP_API_VERSION_ENTERPRISE


async def test_version_failopen_to_legacy_on_error(monkeypatch):
    async def _boom(key, default=None, client_id=None):
        raise RuntimeError("db down")

    monkeypatch.setattr("fashion_bot.config_manager.aget_config", _boom)
    assert await wav.aget_whatsapp_api_version("client-1") == wav.WHATSAPP_API_VERSION_LEGACY


# ---------------------------------------------------------------------------
# Phone / credential helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("7503014404", "917503014404"),
        ("+91 7503014404", "917503014404"),
        ("0917503014404", "917503014404"),
        ("917503014404", "917503014404"),
    ],
)
def test_normalize_msisdn(raw, expected):
    assert wec.normalize_msisdn(raw) == expected


def test_resolve_credentials_canonical_and_aliases():
    canonical = wec._resolve_credentials({
        "GUPSHUP_ENTERPRISE_USERID": "2000266349",
        "GUPSHUP_ENTERPRISE_PASSWORD": "secret123",
        "GUPSHUP_ENTERPRISE_URL": "https://example/api",
    })
    assert canonical == {
        "userid": "2000266349",
        "password": "secret123",
        "url": "https://example/api",
    }

    aliased = wec._resolve_credentials({"userId": "1", "password": "tok"})
    assert aliased["userid"] == "1"
    assert aliased["password"] == "tok"
    assert aliased["url"] == wec.DEFAULT_GATEWAY_URL  # default when absent


# ---------------------------------------------------------------------------
# Form-body construction
# ---------------------------------------------------------------------------

def test_build_template_body_requires_image_url():
    with pytest.raises(ValueError, match="image_url"):
        wec.build_template_params(
            userid="2000266349",
            password="secret123",
            send_to="917503014404",
            template_id="3846481234569972",
            params=["Alice", "ORD-1"],
        )


def test_build_media_template_body():
    body = wec.build_template_params(
        userid="2000266349",
        password="secret123",
        send_to="917503014404",
        template_id="999",
        params=["Alice"],
        image_url="https://cdn.example.com/a b.png?x=1&y=2",
    )
    assert body["method"] == "SendMediaMessage"
    assert body["msg_type"] == "IMAGE"
    assert body["userid"] == "2000266349"
    assert body["password"] == "secret123"
    assert body["send_to"] == "917503014404"
    assert body["whatsAppTemplateId"] == "999"
    assert body["isHSM"] == "true"
    assert body["isTemplate"] == "false"
    assert body["auth_scheme"] == "plain"
    assert body["v"] == "1.1"
    assert body["format"] == "json"
    assert body["media_url"] == "https://cdn.example.com/a b.png?x=1&y=2"
    assert body["var1"] == "Alice"


def test_build_media_template_unquotes_image_url():
    """An already percent-encoded URL must be decoded so urlencode encodes once
    (guards against %20 -> %2520 double-encoding)."""
    body = wec.build_template_params(
        userid="u",
        password="p",
        send_to="917503014404",
        template_id="t",
        image_url="https://cdn.example.com/a%20b.png?c=1%2C2",
    )
    assert body["media_url"] == "https://cdn.example.com/a b.png?c=1,2"
    # On the wire it is single-encoded: no double-encoding, and decoding the
    # form value round-trips back to the canonical URL.
    encoded = urlencode(body)
    assert "%2520" not in encoded  # would indicate %20 was re-encoded
    decoded = {k: v[0] for k, v in parse_qs(encoded, keep_blank_values=True).items()}
    assert decoded["media_url"] == "https://cdn.example.com/a b.png?c=1,2"


def test_build_session_text_body():
    body = wec.build_session_text_params(
        userid="2000266349",
        password="secret-password",
        send_to="917503014404",
        message="Hi there!",
    )
    assert body["method"] == "SendMessage"
    assert body["userid"] == "2000266349"
    assert body["password"] == "secret-password"
    assert body["msg_type"] == "TEXT"
    assert body["msg"] == "Hi there!"
    assert "isHSM" not in body  # session message, not a template


def test_form_body_urlencode_roundtrip():
    """media_url with special chars must survive URL-encode → decode (spec §5)."""
    body = wec.build_template_params(
        userid="u",
        password="p",
        send_to="917503014404",
        template_id="t",
        params=["a&b", "c=d"],
        image_url="https://cdn.example.com/a b.png?x=1&y=2",
    )
    encoded = urlencode(body)
    decoded = {k: v[0] for k, v in parse_qs(encoded, keep_blank_values=True).items()}
    assert decoded == body


def test_resolve_template_params_from_dict_and_param_order():
    params = wec.resolve_template_params(
        {
            "Customer Name": "Alice",
            "Order ID": "ORD-1",
            "Tracking link": "https://track.example/1",
            "Order Value": "1299",
        },
        ["customer_firstname", "order_number", "tracking_link", "total_price"],
    )
    assert params == ["Alice", "ORD-1", "https://track.example/1", "1299"]


@pytest.mark.parametrize("param_order", ["Customer Name,Order ID", "Customer Name, Order ID"])
def test_resolve_template_params_from_comma_separated_param_order(param_order):
    params = wec.resolve_template_params(
        {"Customer Name": "Alice", "Order ID": "ORD-1"},
        param_order,
    )
    assert params == ["Alice", "ORD-1"]


def test_build_template_params_from_order_webhook_context():
    params = build_template_params_from_context(
        "Customer Name,Order ID,Tracking link,Order Value",
        {
            "customer_name": "Alice",
            "order_id": "ORD-1",
            "tracking_link": "https://track.example/ORD-1",
            "order_value": "1299",
        },
    )
    assert params == ["Alice", "ORD-1", "https://track.example/ORD-1", "1299"]


def test_build_template_params_from_context_should_use_empty_string_when_discount_missing():
    # given
    param_order = ["name", "abandoned_checkout_url", "discount"]
    context = {
        "customer_name": "Alice",
        "abandoned_checkout_url": "https://shop.example/checkout/1",
        "product_url": "https://shop.example/checkout/1",
    }

    # when
    params = build_template_params_from_context(param_order, context)

    # then
    assert params == ["Alice", "https://shop.example/checkout/1", ""]


def test_build_template_params_from_context_should_keep_static_literal_when_not_dynamic_key():
    # given / when
    params = build_template_params_from_context(["FREE SHIPPING"], {})

    # then
    assert params == ["FREE SHIPPING"]


def test_resolve_template_params_should_not_raise_when_discount_missing():
    # given
    values = {
        "customer_name": "Alice",
        "abandoned_checkout_url": "https://shop.example/checkout/1",
    }

    # when
    params = resolve_template_params(
        values,
        ["name", "abandoned_checkout_url", "discount"],
    )

    # then
    assert params == ["Alice", "https://shop.example/checkout/1", ""]


def test_build_template_params_from_context_should_use_discount_when_present():
    # given / when
    params = build_template_params_from_context(
        ["name", "discount"],
        {"customer_name": "Alice", "discount": "NEW10"},
    )

    # then
    assert params == ["Alice", "NEW10"]


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"response": {"status": "success", "id": "1"}}, True),
        ({"response": {"status": "error", "id": "1"}}, False),
        ({"response": {"status": "success"}}, False),  # no id
        ({"status": "success"}, False),
        ("nope", False),
    ],
)
def test_is_success_response(payload, expected):
    assert wec._is_success_response(payload) is expected


# ---------------------------------------------------------------------------
# Enterprise send end-to-end (mocked transport)
# ---------------------------------------------------------------------------

async def test_asend_enterprise_template_posts_to_gateway(monkeypatch):
    fake_http = _FakeHTTPClient()

    async def _fake_get_client():
        return fake_http

    async def _fake_cfg(client_id):
        return {
            "userId": "2000266349",
            "password": "secret-password",
        }

    monkeypatch.setattr(wec, "get_shared_async_http_client", _fake_get_client)
    monkeypatch.setattr(wec, "aget_enterprise_text_config", _fake_cfg)

    result = await wec.asend_enterprise_template(
        "7503014404",
        "tmpl-1",
        ["Alice"],
        image_url="https://cdn.example.com/a.png",
        client_id="gant",
    )

    assert result["response"]["status"] == "success"
    assert len(fake_http.calls) == 1
    call = fake_http.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == wec.DEFAULT_GATEWAY_URL
    sent = call["params"]
    assert sent["userid"] == "2000266349"
    assert sent["password"] == "secret-password"
    assert sent["send_to"] == "917503014404"
    assert sent["whatsAppTemplateId"] == "tmpl-1"
    assert sent["method"] == "SendMediaMessage"
    assert sent["msg_type"] == "IMAGE"
    assert sent["media_url"] == "https://cdn.example.com/a.png"
    assert sent["isTemplate"] == "false"
    assert sent["var1"] == "Alice"


async def test_asend_enterprise_text_posts_form_credentials(monkeypatch):
    fake_http = _FakeHTTPClient()

    async def _fake_get_client():
        return fake_http

    async def _fake_cfg(client_id):
        return {
            "USERID": "2000266260",
            "PASSWORD": "EL7OTPEzI",
        }

    monkeypatch.setattr(wec, "get_shared_async_http_client", _fake_get_client)
    monkeypatch.setattr(wec, "aget_enterprise_config", _fake_cfg)

    result = await wec.asend_enterprise_text_message(
        "9611703832",
        "This is test message",
        client_id="enterprise-client",
    )

    assert result["response"]["status"] == "success"
    assert len(fake_http.calls) == 1
    call = fake_http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == wec.DEFAULT_GATEWAY_URL
    assert "Authorization" not in call["headers"]
    sent = {k: v[0] for k, v in parse_qs(call["data"]).items()}
    assert sent["send_to"] == "919611703832"
    assert sent["msg_type"] == "TEXT"
    assert sent["userid"] == "2000266260"
    assert sent["password"] == "EL7OTPEzI"
    assert sent["auth_scheme"] == "plain"
    assert sent["method"] == "SendMessage"
    assert sent["v"] == "1.1"
    assert sent["format"] == "json"
    assert sent["msg"] == "This is test message"


async def test_asend_enterprise_returns_none_without_config(monkeypatch):
    async def _no_cfg(client_id):
        return None

    monkeypatch.setattr(wec, "aget_enterprise_text_config", _no_cfg)
    assert await wec.asend_enterprise_text_message("7503014404", "hi", client_id="x") is None


# ---------------------------------------------------------------------------
# Dispatch from the existing send functions
# ---------------------------------------------------------------------------

async def test_template_sender_routes_to_enterprise(monkeypatch):
    """Enterprise tenant: routes to the enterprise transport AND still runs the
    shared side effects (delivery logging + conversation history), with the
    message_id extracted from the GatewayAPI {"response":{"id":...}} shape."""
    from fashion_bot.shipping.webhook import gupshup_template_sender as gts

    async def _enterprise_version(client_id, *a, **kw):
        return wav.WHATSAPP_API_VERSION_ENTERPRISE

    captured = {}

    async def _fake_enterprise_template(destination_phone, template_id, params, **kwargs):
        captured["args"] = (destination_phone, template_id, params, kwargs)
        return {"response": {"status": "success", "id": "ENT-123"}}

    # If the legacy path were taken it would call aget_gupshup_config — make that
    # explode so the test fails loudly on mis-routing.
    async def _legacy_must_not_run(client_id=None):
        raise AssertionError("legacy path should not run for enterprise tenant")

    # Hermetic stubs for the transport-agnostic render + side-effect helpers.
    async def _fake_fetch(template_id, client_id=None):
        return {"id": template_id}

    def _fake_render(template_def, params):
        return "rendered preview"

    async def _fake_log(**kwargs):
        captured["delivery_log"] = kwargs

    async def _fake_conv(**kwargs):
        captured["conversation"] = kwargs
        return "conv-1"

    monkeypatch.setattr(
        "fashion_bot.utils.whatsapp_api_version.aget_whatsapp_api_version",
        _enterprise_version,
    )
    monkeypatch.setattr(
        "fashion_bot.utils.whatsapp_enterprise_client.asend_enterprise_template",
        _fake_enterprise_template,
    )
    monkeypatch.setattr(gts, "aget_gupshup_config", _legacy_must_not_run)
    monkeypatch.setattr(
        "fashion_bot.utils.gupshup_api_client.afetch_template_from_gupshup", _fake_fetch
    )
    monkeypatch.setattr(
        "fashion_bot.utils.gupshup_api_client.render_template_message", _fake_render
    )
    monkeypatch.setattr(
        "fashion_bot.utils.template_delivery_logger.alog_template_delivery", _fake_log
    )
    monkeypatch.setattr(
        "fashion_bot.utils.conversation_message_adder.aadd_template_to_conversation_safe",
        _fake_conv,
    )

    result = await gts.asend_gupshup_template_generic(
        "7503014404", "tmpl-9", ["A"], client_id="gant", event_key="DELIVERED"
    )
    assert result == {"response": {"status": "success", "id": "ENT-123"}}
    assert captured["args"][0] == "7503014404"
    assert captured["args"][1] == "tmpl-9"
    # Side-effect parity: delivery logged with enterprise message_id + success.
    assert captured["delivery_log"]["message_id"] == "ENT-123"
    assert captured["delivery_log"]["success"] is True
    assert captured["delivery_log"]["event_key"] == "DELIVERED"
    # Conversation history updated with the rendered template.
    assert captured["conversation"]["template_message"] == "rendered preview"


async def test_template_sender_legacy_does_not_call_enterprise(monkeypatch):
    from fashion_bot.shipping.webhook import gupshup_template_sender as gts

    async def _legacy_version(client_id, *a, **kw):
        return wav.WHATSAPP_API_VERSION_LEGACY

    async def _enterprise_must_not_run(*a, **kw):
        raise AssertionError("enterprise path should not run for legacy tenant")

    # Short-circuit the legacy network path after the version check.
    async def _no_legacy_cfg(client_id=None):
        return None

    monkeypatch.setattr(
        "fashion_bot.utils.whatsapp_api_version.aget_whatsapp_api_version",
        _legacy_version,
    )
    monkeypatch.setattr(
        "fashion_bot.utils.whatsapp_enterprise_client.asend_enterprise_template",
        _enterprise_must_not_run,
    )
    monkeypatch.setattr(gts, "aget_gupshup_config", _no_legacy_cfg)

    fake_http = _FakeHTTPClient(_FakeResponse(payload={"messageId": "x"}))

    async def _fake_get_client():
        return fake_http

    monkeypatch.setattr(gts, "get_shared_async_http_client", _fake_get_client)

    # Should run the legacy path (posts to the v1 endpoint), not enterprise.
    await gts.asend_gupshup_template_generic("7503014404", "tmpl", ["A"], client_id="legacy")
    assert fake_http.calls and fake_http.calls[0]["method"] == "POST"
    assert "api.gupshup.io" in fake_http.calls[0]["url"]
    sent = {k: v[0] for k, v in parse_qs(fake_http.calls[0]["data"]).items()}
    assert json.loads(sent["template"]) == {"id": "tmpl", "params": ["A"]}
    assert "password" not in sent
    assert "whatsAppTemplateId" not in sent


async def test_shopify_template_sender_routes_to_enterprise(monkeypatch):
    from fashion_bot.shopify.webhook import gupshup_template_sender as sgts

    async def _enterprise_version(client_id, *a, **kw):
        return wav.WHATSAPP_API_VERSION_ENTERPRISE

    captured = {}

    async def _fake_enterprise_template(destination_phone, template_id, params, **kwargs):
        captured["args"] = (destination_phone, template_id, params, kwargs)
        return {"response": {"status": "success", "id": "SHOP-ENT-1"}}

    async def _legacy_must_not_run(client_id=None):
        raise AssertionError("legacy Shopify path should not read legacy config")

    async def _fake_fetch(template_id, client_id=None):
        return {"id": template_id}

    def _fake_render(template_def, params):
        return "rendered shopify preview"

    async def _fake_log(**kwargs):
        captured["delivery_log"] = kwargs

    async def _fake_conv(**kwargs):
        captured["conversation"] = kwargs
        return "conv-shopify"

    monkeypatch.setattr(
        "fashion_bot.utils.whatsapp_api_version.aget_whatsapp_api_version",
        _enterprise_version,
    )
    monkeypatch.setattr(
        "fashion_bot.utils.whatsapp_enterprise_client.asend_enterprise_template",
        _fake_enterprise_template,
    )
    monkeypatch.setattr(sgts, "aget_gupshup_config", _legacy_must_not_run)
    monkeypatch.setattr(
        "fashion_bot.utils.gupshup_api_client.afetch_template_from_gupshup", _fake_fetch
    )
    monkeypatch.setattr(
        "fashion_bot.utils.gupshup_api_client.render_template_message", _fake_render
    )
    monkeypatch.setattr(
        "fashion_bot.utils.template_delivery_logger.alog_template_delivery", _fake_log
    )
    monkeypatch.setattr(
        "fashion_bot.utils.conversation_message_adder.aadd_template_to_conversation_safe",
        _fake_conv,
    )

    result = await sgts.asend_shopify_gupshup_template_generic(
        "7503014404",
        "tmpl-shopify",
        ["Alice", "ORD-1"],
        image_url="https://cdn.example.com/a.png",
        client_id="enterprise-client",
        event_key="ORDER_CONFIRMED",
        template_name="order_confirmed",
        order_id="ORD-1",
    )

    assert result == {"response": {"status": "success", "id": "SHOP-ENT-1"}}
    assert captured["args"][0] == "7503014404"
    assert captured["args"][1] == "tmpl-shopify"
    assert captured["args"][2] == ["Alice", "ORD-1"]
    assert captured["args"][3]["image_url"] == "https://cdn.example.com/a.png"
    assert captured["args"][3]["log_tag"] == "order_confirmed"
    assert captured["delivery_log"]["message_id"] == "SHOP-ENT-1"
    assert captured["delivery_log"]["success"] is True


async def test_shopify_template_sender_legacy_posts_v1(monkeypatch):
    from fashion_bot.shopify.webhook import gupshup_template_sender as sgts

    async def _legacy_version(client_id, *a, **kw):
        return wav.WHATSAPP_API_VERSION_LEGACY

    async def _enterprise_must_not_run(*a, **kw):
        raise AssertionError("enterprise path should not run for legacy Shopify tenant")

    async def _no_legacy_cfg(client_id=None):
        return None

    monkeypatch.setattr(
        "fashion_bot.utils.whatsapp_api_version.aget_whatsapp_api_version",
        _legacy_version,
    )
    monkeypatch.setattr(
        "fashion_bot.utils.whatsapp_enterprise_client.asend_enterprise_template",
        _enterprise_must_not_run,
    )
    monkeypatch.setattr(sgts, "aget_gupshup_config", _no_legacy_cfg)

    fake_http = _FakeHTTPClient(_FakeResponse(payload={"messageId": "shopify-v1"}))

    async def _fake_get_client():
        return fake_http

    monkeypatch.setattr(sgts, "get_shared_async_http_client", _fake_get_client)

    await sgts.asend_shopify_gupshup_template_generic(
        "7503014404", "tmpl-shopify", ["Alice"], client_id="legacy-shopify"
    )
    assert fake_http.calls and fake_http.calls[0]["method"] == "POST"
    assert "api.gupshup.io" in fake_http.calls[0]["url"]
    sent = {k: v[0] for k, v in parse_qs(fake_http.calls[0]["data"]).items()}
    assert json.loads(sent["template"]) == {"id": "tmpl-shopify", "params": ["Alice"]}
    assert "password" not in sent
    assert "whatsAppTemplateId" not in sent


# ---------------------------------------------------------------------------
# Live integration test — sends a real template to 7503014404
# ---------------------------------------------------------------------------

LIVE_RECIPIENT = "7503014404"


def _confirm_received(label: str) -> None:
    """Pause and ask the operator to confirm receipt of the just-sent message.

    Requires ``pytest -s`` so stdin/stdout are not captured. Set
    ``GANT_TEST_ASSUME_YES=1`` to auto-confirm in non-interactive runs.
    """
    from fashion_bot.env_loader import get_env

    if str(get_env("GANT_TEST_ASSUME_YES") or "").lower() in {"1", "true", "yes"}:
        print(f"  → GANT_TEST_ASSUME_YES set; auto-confirming receipt of {label}")
        return
    answer = input(
        f"\n>>> Did you RECEIVE the {label} on {LIVE_RECIPIENT}? (y/n): "
    ).strip().lower()
    assert answer in {"y", "yes"}, f"Operator did NOT confirm receipt of {label}"


@pytest.mark.integration
async def test_live_send_to_gant_with_confirmation(monkeypatch):
    """Send real enterprise message(s) to 7503014404 using gant .env creds.

    Sends one message at a time, prints the send + raw gateway response to the
    console, and pauses to take your confirmation that you received it.

    Skipped unless gant_client / gant_secret are in the environment and an
    approved GANT_TEST_TEMPLATE_ID is provided. Optional:
      * GANT_TEST_TEMPLATE_PARAMS  – comma-separated template variable values
      * GANT_TEST_IMAGE_URL        – media URL (makes it a media template)
      * GANT_TEST_SEND_TEXT=1      – also send a free-text session reply
      * GANT_TEST_ASSUME_YES=1     – auto-confirm (non-interactive)

    Run interactively (note the -s so prompts are visible)::

        GANT_TEST_TEMPLATE_ID=<approved_id> \\
        pytest -m integration -s tests/test_whatsapp_api_versioning.py -k live
    """
    from fashion_bot.env_loader import get_env

    userid = get_env("gant_client")
    token = get_env("gant_secret")
    # Default to IconicIndia's order-DELIVERED template (prod) — overridable via env.
    # Verified live: this template actually delivers to the handset.
    template_id = get_env("GANT_TEST_TEMPLATE_ID") or "bbce0619-dd49-4998-ae27-b0f646c82881"
    if not (userid and token):
        pytest.skip("Set gant_client + gant_secret in .env to run the live send")

    params_raw = get_env("GANT_TEST_TEMPLATE_PARAMS")
    if params_raw is None:
        # IconicIndia order-delivered params: Customer Name, Order ID
        params = ["Test User", "TEST-001"]
    else:
        params = [p.strip() for p in params_raw.split(",") if p.strip()]
    image_url = get_env("GANT_TEST_IMAGE_URL") or None

    async def _gant_cfg(client_id):
        return {
            "GUPSHUP_ENTERPRISE_USERID": userid,
            "GUPSHUP_ENTERPRISE_TOKEN": token,
        }

    async def _enterprise_version(client_id, *a, **kw):
        return wav.WHATSAPP_API_VERSION_ENTERPRISE

    monkeypatch.setattr(wec, "aget_enterprise_config", _gant_cfg)
    monkeypatch.setattr(wec, "aget_enterprise_text_config", _gant_cfg)
    monkeypatch.setattr(
        "fashion_bot.utils.whatsapp_api_version.aget_whatsapp_api_version",
        _enterprise_version,
    )

    from fashion_bot.shipping.webhook.gupshup_template_sender import (
        asend_gupshup_template_generic,
    )

    # ---- Message 1: template (HSM) --------------------------------------
    print(
        f"\n[LIVE] Sending TEMPLATE id={template_id} params={params} "
        f"image_url={image_url} to {LIVE_RECIPIENT} ..."
    )
    result = await asend_gupshup_template_generic(
        LIVE_RECIPIENT,
        template_id,
        params,
        image_url=image_url,
        client_id="gant-live-test",
    )
    print("[LIVE] gateway raw response:", result)
    assert result is not None, "Template send failed — see captured logs for gateway error"
    assert result["response"]["status"] == "success"
    # Real gateway ids look like "5723377696826470512-441991391311480406"
    # (two numbers joined by a hyphen) — assert non-empty, not all-digits.
    assert result["response"]["id"]
    _confirm_received("TEMPLATE message")

    # ---- Message 2 (optional): free-text session reply ------------------
    if str(get_env("GANT_TEST_SEND_TEXT") or "").lower() in {"1", "true", "yes"}:
        text = get_env("GANT_TEST_TEXT") or "Hello from the enterprise WhatsApp test ✅"
        print(f"\n[LIVE] Sending TEXT '{text}' to {LIVE_RECIPIENT} ...")
        text_result = await wec.asend_enterprise_text_message(
            LIVE_RECIPIENT, text, client_id="gant-live-test"
        )
        print("[LIVE] gateway raw response:", text_result)
        assert text_result is not None, (
            "Text send failed — note free-text needs an open 24h session window"
        )
        assert text_result["response"]["status"] == "success"
        _confirm_received("TEXT message")


@pytest.mark.integration
async def test_live_send_text_hsm_to_gant_with_confirmation(monkeypatch):
    """Verify the enterprise **text-only HSM** wire format against the live gateway.

    This is the send used for enterprise escalation templates that have no image
    header (``asend_enterprise_text_template``). Skipped unless gant creds and an
    approved TEXT (non-media) template id are provided. Use this to confirm the
    ``method``/``msg_type`` before enabling enterprise text-only escalation
    templates in production.

        GANT_TEXT_HSM_TEMPLATE_ID=<approved_text_template_id> \\
        GANT_TEXT_HSM_PARAMS="Test User,TEST-001" \\
        pytest -m integration -s tests/test_whatsapp_api_versioning.py -k text_hsm
    """
    from fashion_bot.env_loader import get_env

    userid = get_env("gant_client")
    token = get_env("gant_secret")
    template_id = get_env("GANT_TEXT_HSM_TEMPLATE_ID")
    if not (userid and token and template_id):
        pytest.skip(
            "Set gant_client + gant_secret + GANT_TEXT_HSM_TEMPLATE_ID (an approved "
            "TEXT template) to run the text-HSM live send"
        )

    params_raw = get_env("GANT_TEXT_HSM_PARAMS")
    params = (
        [p.strip() for p in params_raw.split(",") if p.strip()]
        if params_raw is not None
        else ["Test User", "TEST-001"]
    )

    async def _gant_cfg(client_id):
        cfg = {
            "GUPSHUP_ENTERPRISE_USERID": userid,
            "GUPSHUP_ENTERPRISE_TOKEN": token,
        }
        # Allow overriding the wire values from env while pinning the format.
        method = get_env("GANT_TEXT_HSM_METHOD")
        msg_type = get_env("GANT_TEXT_HSM_MSG_TYPE")
        if method:
            cfg["text_hsm_method"] = method
        if msg_type:
            cfg["text_hsm_msg_type"] = msg_type
        return cfg

    monkeypatch.setattr(wec, "aget_enterprise_config", _gant_cfg)

    print(
        f"\n[LIVE] Sending TEXT HSM id={template_id} params={params} "
        f"to {LIVE_RECIPIENT} ..."
    )
    result = await wec.asend_enterprise_text_template(
        LIVE_RECIPIENT, template_id, params, client_id="gant-live-test"
    )
    print("[LIVE] gateway raw response:", result)
    assert result is not None, (
        "Text-HSM send failed — inspect the gateway error and adjust "
        "text_hsm_method / text_hsm_msg_type"
    )
    assert result["response"]["status"] == "success"
    assert result["response"]["id"]
    _confirm_received("TEXT HSM (template) message")
