"""Unit tests for opt-in escalation delivery via Gupshup templates.

Covers (design_docs/ESCALATION_GUPSHUP_TEMPLATES.md §10):
  * ``amaybe_send_escalation_template`` outcomes: not-configured / disabled,
    legacy send success, enterprise phase-1 skip, gateway failure, pre-send
    failures (no creds / bad param_order), static (no-param) template.
  * Parameter ordering onto ``var1..varN`` and the newline/whitespace
    sanitization contract (never raises, never emits ``None``, length caps).
  * ``build_escalation_template_fields`` canonical output + ``priority`` urgency.
  * ``asend_escalation_notification`` per-recipient wiring: template-accepted →
    no free-text second send (no double-alert); gateway-failed → ``degraded`` +
    email still fires; pre-send/not-configured → free-text fallback; and no
    ``details`` ⇒ free-text only.
"""
from __future__ import annotations

import json
import sys
import types
from urllib.parse import parse_qs

import fashion_bot.agent_config as ac
from fashion_bot import config_manager
from fashion_bot.utils import escalation_template_sender as ets
from fashion_bot.utils.escalation_template_sender import (
    TemplateSendOutcome,
    amaybe_send_escalation_template,
)
from fashion_bot.utils.escalation_helper import build_escalation_template_fields


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #

def _patch_config(monkeypatch, configs):
    """Serve config_manager.aget_config from an in-memory dict."""

    async def _fake_aget_config(config_key, default=None, client_id=None):
        return configs.get(config_key, default)

    monkeypatch.setattr(config_manager, "aget_config", _fake_aget_config)


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text="OK"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class _FakeHTTP:
    def __init__(self, status_code=200, payload=None):
        self.calls = []
        self._status_code = status_code
        self._payload = payload if payload is not None else {"status": "submitted"}

    async def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers})
        return _FakeResp(self._status_code, self._payload)


def _patch_http(monkeypatch, client):
    async def _get_client():
        return client

    monkeypatch.setattr(ets, "get_shared_async_http_client", _get_client)


_LEGACY_CREDS = {
    "GUPSHUP_TEMPLATE_API_KEY": "key123",
    "GUPSHUP_TEMPLATE_SOURCE": "919999999999",
    "GUPSHUP_TEMPLATE_URL": "https://api.gupshup.io/wa/api/v1/template/msg",
    "APP_NAME": "TestApp",
}

_FIELDS = {
    "priority": "",
    "category": "Return Request",
    "customer_contact": "9876543210",
    "order_id": "1001",
    "summary": "Customer wants to return item",
}


def _decode_template(call):
    """Pull the decoded ``template`` JSON out of a captured POST call."""
    parsed = parse_qs(call["data"])
    return json.loads(parsed["template"][0])


# --------------------------------------------------------------------------- #
# amaybe_send_escalation_template
# --------------------------------------------------------------------------- #

_ROUTE_TMPL = {"template_id": "tmpl-123"}


async def test_not_configured_when_no_route_template(monkeypatch):
    _patch_config(monkeypatch, {})
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS
    )
    assert outcome == TemplateSendOutcome.NOT_CONFIGURED


async def test_not_configured_when_route_template_empty(monkeypatch):
    _patch_config(monkeypatch, {})
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={},
    )
    assert outcome == TemplateSendOutcome.NOT_CONFIGURED


async def test_not_configured_without_template_fields(monkeypatch):
    _patch_config(monkeypatch, {})
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=None,
        route_template=_ROUTE_TMPL,
    )
    assert outcome == TemplateSendOutcome.NOT_CONFIGURED


async def test_legacy_send_success_and_param_order(monkeypatch):
    http = _FakeHTTP(status_code=200, payload={"status": "submitted", "messageId": "m1"})
    _patch_http(monkeypatch, http)
    _patch_config(monkeypatch, {
        "escalation_template": {
            "param_order": ["priority", "category", "customer_contact", "order_id", "summary"],
        },
        "gupshup_template_details": _LEGACY_CREDS,
    })

    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "tmpl-123"},
    )
    assert outcome == TemplateSendOutcome.SENT
    assert len(http.calls) == 1
    tmpl = _decode_template(http.calls[0])
    assert tmpl["id"] == "tmpl-123"
    assert tmpl["params"] == ["", "Return Request", "9876543210", "1001", "Customer wants to return item"]


def _patch_enterprise(monkeypatch, *, creds=True, text_result="ok", media_result="ok"):
    """Patch the enterprise client seam used by ``_asend_enterprise``."""
    from fashion_bot.utils import whatsapp_enterprise_client as wec

    calls = {"text": [], "media": []}

    async def _cfg(client_id):
        if not creds:
            return None
        return {"GUPSHUP_ENTERPRISE_USERID": "u", "GUPSHUP_ENTERPRISE_PASSWORD": "p"}

    async def _text(dest, template_id, params=None, client_id=None, **kw):
        calls["text"].append({"template_id": template_id, "params": params})
        return {"response": {"status": "success", "id": "1"}} if text_result == "ok" else None

    async def _media(dest, template_id, params=None, image_url=None, client_id=None, **kw):
        calls["media"].append({"template_id": template_id, "params": params, "image_url": image_url})
        return {"response": {"status": "success", "id": "1"}} if media_result == "ok" else None

    monkeypatch.setattr(wec, "aget_enterprise_config", _cfg)
    monkeypatch.setattr(wec, "asend_enterprise_text_template", _text)
    monkeypatch.setattr(wec, "asend_enterprise_template", _media)
    return calls


async def test_enterprise_text_hsm_routes_to_text_sender(monkeypatch):
    calls = _patch_enterprise(monkeypatch)
    _patch_config(monkeypatch, {
        "escalation_template": {
            "param_order": ["priority", "category", "customer_contact", "order_id", "summary"],
        },
        "whatsapp_api_version": "enterprise",
    })
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "ent-tmpl"},
    )
    assert outcome == TemplateSendOutcome.SENT
    assert len(calls["text"]) == 1 and calls["media"] == []
    assert calls["text"][0]["template_id"] == "ent-tmpl"
    assert calls["text"][0]["params"] == ["", "Return Request", "9876543210", "1001", "Customer wants to return item"]


async def test_enterprise_media_routes_to_media_sender(monkeypatch):
    calls = _patch_enterprise(monkeypatch)
    _patch_config(monkeypatch, {
        "escalation_template": {"param_order": ["category"]},
        "whatsapp_api_version": "enterprise",
    })
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "ent-media", "image_url": "https://cdn.example.com/alert.png"},
    )
    assert outcome == TemplateSendOutcome.SENT
    assert len(calls["media"]) == 1 and calls["text"] == []
    assert calls["media"][0]["image_url"] == "https://cdn.example.com/alert.png"


async def test_enterprise_missing_creds_is_presend(monkeypatch):
    calls = _patch_enterprise(monkeypatch, creds=False)
    _patch_config(monkeypatch, {
        "escalation_template": {"param_order": ["category"]},
        "whatsapp_api_version": "enterprise",
    })
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "t1"},
    )
    assert outcome == TemplateSendOutcome.FAILED_PRESEND
    assert calls["text"] == [] and calls["media"] == []


async def test_enterprise_gateway_failure(monkeypatch):
    _patch_enterprise(monkeypatch, text_result="fail")
    _patch_config(monkeypatch, {
        "escalation_template": {"param_order": ["category"]},
        "whatsapp_api_version": "enterprise",
    })
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "t1"},
    )
    assert outcome == TemplateSendOutcome.GATEWAY_FAILED


def test_build_text_template_params_wire_shape():
    from fashion_bot.utils.whatsapp_enterprise_client import build_text_template_params

    body = build_text_template_params(
        userid="u", password="p", send_to="919111111111",
        template_id="tmpl-9", params=["Alice", "1001"],
    )
    assert body["method"] == "SendMessage"
    assert body["msg_type"] == "HSM"
    assert body["isHSM"] == "true"
    assert body["isTemplate"] == "false"
    assert body["whatsAppTemplateId"] == "tmpl-9"
    assert body["var1"] == "Alice" and body["var2"] == "1001"
    assert "media_url" not in body  # text-only: no media header


async def test_gateway_failure_returns_gateway_failed(monkeypatch):
    http = _FakeHTTP(status_code=500, payload=None)
    _patch_http(monkeypatch, http)
    _patch_config(monkeypatch, {
        "escalation_template": {"param_order": ["category"]},
        "gupshup_template_details": _LEGACY_CREDS,
    })
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "t1"},
    )
    assert outcome == TemplateSendOutcome.GATEWAY_FAILED
    assert len(http.calls) == 1


async def test_presend_failure_missing_creds(monkeypatch):
    http = _FakeHTTP()
    _patch_http(monkeypatch, http)
    _patch_config(monkeypatch, {
        "escalation_template": {"param_order": ["category"]},
        # no gupshup_template_details
    })
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "t1"},
    )
    assert outcome == TemplateSendOutcome.FAILED_PRESEND
    assert http.calls == []  # never reached the gateway


async def test_presend_failure_bad_param_order(monkeypatch):
    http = _FakeHTTP()
    _patch_http(monkeypatch, http)
    _patch_config(monkeypatch, {
        "escalation_template": {
            "param_order": ["totally_unknown_label"],
        },
        "gupshup_template_details": _LEGACY_CREDS,
    })
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "t1"},
    )
    assert outcome == TemplateSendOutcome.FAILED_PRESEND
    assert http.calls == []


async def test_static_template_without_param_order(monkeypatch):
    http = _FakeHTTP(status_code=200)
    _patch_http(monkeypatch, http)
    _patch_config(monkeypatch, {
        "gupshup_template_details": _LEGACY_CREDS,
        # no escalation_template → no param_order → static template
    })
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "static-1"},
    )
    assert outcome == TemplateSendOutcome.SENT
    tmpl = _decode_template(http.calls[0])
    assert tmpl["params"] == []


# --------------------------------------------------------------------------- #
# Sanitization
# --------------------------------------------------------------------------- #

def test_sanitize_param_collapses_whitespace():
    out = ets.sanitize_template_param("a\tb\n\nc     d")
    assert out == "a b c d"
    assert "\n" not in out and "\t" not in out


def test_sanitize_param_length_cap():
    out = ets.sanitize_template_param("x" * 500, cap=10)
    assert len(out) <= 10
    assert out.endswith("…")


def test_sanitize_param_none_is_empty():
    assert ets.sanitize_template_param(None) == ""


def test_sanitize_summary_newlines_to_bullets_and_emoji():
    out = ets.sanitize_template_summary("line1\nline2\n\nline3 😀")
    assert "line1 · line2 · line3 😀" == out
    assert "\n" not in out


def test_sanitize_summary_length_cap():
    out = ets.sanitize_template_summary("y" * 2000, cap=50)
    assert len(out) <= 50


def test_sanitize_summary_strips_control_chars():
    assert "\x07" not in ets.sanitize_template_summary("bell\x07here")


# --------------------------------------------------------------------------- #
# build_escalation_template_fields
# --------------------------------------------------------------------------- #

def test_build_fields_never_empty_and_priority():
    fields = build_escalation_template_fields(
        "Cancellation Requests",
        None,               # order_id absent
        None,               # phone_number absent
        "urgent\ndetails",
        escalation_group="post_sales",
        immediate_attention=True,
    )
    # Every value must be a non-empty string — Meta rejects blank template params.
    assert all(isinstance(v, str) and v != "" for v in fields.values())
    assert fields["priority"] == "URGENT"
    assert fields["order_id"] == "N/A"       # absent → safe token, not ""
    assert fields["customer_name"] == "N/A"
    assert fields["customer_contact"] == "web chat"
    assert fields["escalation_group"] == "Post Sales"
    assert fields["conversation"] == "N/A"   # no messages → safe token, not ""
    assert "\n" not in fields["summary"]


def test_build_fields_non_urgent_priority_and_override():
    fields = build_escalation_template_fields(
        "Return Request", "1001", "919876543210", "details",
        customer_contact="jane@example.com",
    )
    assert fields["priority"] == "Normal"    # non-urgent → "Normal", never ""
    assert fields["customer_contact"] == "jane@example.com"
    assert fields["order_id"] == "1001"
    assert all(v != "" for v in fields.values())


def test_build_fields_conversation_and_name():
    fields = build_escalation_template_fields(
        "Offline Store Suggestion", None, "9716336096",
        "Store: Concept Groove\nAddress: E-9 Jail Road\nDistance: ~2.1 km",
        escalation_group="offline_leads",
        customer_name="Prabhjot Singh",
        recent_customer_messages=["Do you have offline stores?", "110018"],
    )
    assert fields["customer_name"] == "Prabhjot Singh"
    assert fields["customer_contact"] == "9716336096"
    assert fields["escalation_group"] == "Offline Leads"
    # multi-line details collapse to a single sanitized line
    assert "\n" not in fields["summary"] and " · " in fields["summary"]
    # recent messages joined into one non-empty conversation line
    assert fields["conversation"] == "Do you have offline stores? / 110018"
    assert all(v != "" for v in fields.values())


# --------------------------------------------------------------------------- #
# asend_escalation_notification wiring (template ↔ free-text)
# --------------------------------------------------------------------------- #

def _install_fake_gupshup(monkeypatch, sent, fail_numbers=()):
    mod = types.ModuleType("fashion_bot.gupshup_webhook")

    async def _send_message(to, message, trace_id=None, gupshup_source=None, client_id=None):
        if to in fail_numbers:
            raise RuntimeError(f"boom {to}")
        sent.append((to, message))
        return {"ok": True}

    mod.send_message = _send_message
    monkeypatch.setitem(sys.modules, "fashion_bot.gupshup_webhook", mod)


def _patch_contacts(monkeypatch, phone, email=None, template=None):
    async def _fake_contacts(client_id, *, agent=None, category=None, parent_intent=None):
        return {"phone": phone, "email": email or {"to": [], "cc": []}, "template": template}

    monkeypatch.setattr(ac, "aget_escalation_contacts", _fake_contacts)


def _patch_outcome(monkeypatch, outcome):
    calls = []

    async def _fake(*, to, client_id, template_fields, immediate_attention=False, trace_id=None, route_template=None):
        calls.append({"to": to, "route_template": route_template})
        return outcome

    monkeypatch.setattr(ets, "amaybe_send_escalation_template", _fake)
    return calls


async def test_notification_template_sent_no_free_text(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent)
    _patch_contacts(monkeypatch, ["9111111111"])
    tmpl_calls = _patch_outcome(monkeypatch, TemplateSendOutcome.SENT)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    result = await asend_escalation_notification(
        "body", client_id="c1", category="Return Request", details="summary text"
    )
    assert result["sent"] == ["9111111111"]
    assert result["whatsapp_channel"]["9111111111"] == "template"
    assert tmpl_calls[0]["to"] == "9111111111"
    assert sent == []  # NO free-text second send → no double alert


async def test_notification_gateway_failed_is_degraded_and_emails(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent)
    _patch_contacts(monkeypatch, ["9111111111"], email={"to": ["ops@x.com"], "cc": []})
    _patch_outcome(monkeypatch, TemplateSendOutcome.GATEWAY_FAILED)

    emailed = {}

    async def _fake_email(notification, email, *, subject, trace_id, html_body=None):
        emailed["to"] = email.get("to")

    import fashion_bot.utils.escalation_helper as el
    monkeypatch.setattr(el, "_asend_escalation_email", _fake_email)

    result = await el.asend_escalation_notification(
        "body", client_id="c1", category="Return Request", details="summary text"
    )
    assert result["degraded"] == ["9111111111"]
    assert result["sent"] == []
    assert sent == []                       # gateway-failed does NOT fall back to free text
    assert emailed["to"] == ["ops@x.com"]   # email is the guaranteed channel


async def test_notification_presend_falls_back_to_free_text(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent)
    _patch_contacts(monkeypatch, ["9111111111"])
    _patch_outcome(monkeypatch, TemplateSendOutcome.FAILED_PRESEND)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    result = await asend_escalation_notification(
        "body", client_id="c1", category="Return Request", details="summary text"
    )
    assert result["sent"] == ["9111111111"]
    assert result["whatsapp_channel"]["9111111111"] == "text"
    assert sent == [("9111111111", "body")]  # free-text fallback happened


async def test_notification_not_configured_uses_free_text(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent)
    _patch_contacts(monkeypatch, ["9111111111"])
    _patch_outcome(monkeypatch, TemplateSendOutcome.NOT_CONFIGURED)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    result = await asend_escalation_notification(
        "body", client_id="c1", category="Return Request", details="summary text"
    )
    assert result["sent"] == ["9111111111"]
    assert result["whatsapp_channel"]["9111111111"] == "text"
    assert sent == [("9111111111", "body")]


async def test_notification_no_details_is_free_text_only(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent)
    _patch_contacts(monkeypatch, ["9111111111"])

    called = {"n": 0}

    async def _must_not_call(**kwargs):
        called["n"] += 1
        return TemplateSendOutcome.SENT

    monkeypatch.setattr(ets, "amaybe_send_escalation_template", _must_not_call)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    # No ``details`` → not template-eligible → template sender never consulted.
    result = await asend_escalation_notification("body", client_id="c1", category="General")
    assert result["sent"] == ["9111111111"]
    assert result["whatsapp_channel"]["9111111111"] == "text"
    assert called["n"] == 0
    assert sent == [("9111111111", "body")]


async def test_notification_multi_recipient_mixed_outcomes(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent)
    _patch_contacts(monkeypatch, ["9111111111", "9222222222"])

    async def _fake(*, to, client_id, template_fields, immediate_attention=False, trace_id=None, route_template=None):
        return (
            TemplateSendOutcome.SENT if to == "9111111111"
            else TemplateSendOutcome.GATEWAY_FAILED
        )

    monkeypatch.setattr(ets, "amaybe_send_escalation_template", _fake)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    result = await asend_escalation_notification(
        "body", client_id="c1", category="Return Request", details="summary"
    )
    assert result["sent"] == ["9111111111"]
    assert result["degraded"] == ["9222222222"]
    assert sent == []  # neither recipient double-alerted via free text


# --------------------------------------------------------------------------- #
# Per-route template: route_template forwarding + precedence over global
# --------------------------------------------------------------------------- #


async def test_route_template_forwarded_to_sender(monkeypatch):
    """When contacts carry a resolved route_template, it is forwarded through
    the payload to ``amaybe_send_escalation_template``."""
    sent = []
    _install_fake_gupshup(monkeypatch, sent)
    route_tmpl = {
        "template_id": "route_tmpl_123",
        "image_url": None,
    }
    _patch_contacts(monkeypatch, ["9111111111"], template=route_tmpl)
    tmpl_calls = _patch_outcome(monkeypatch, TemplateSendOutcome.SENT)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    await asend_escalation_notification(
        "body", client_id="c1", category="Return Request", details="summary text"
    )
    assert tmpl_calls[0]["route_template"] == route_tmpl


async def test_route_template_none_when_not_resolved(monkeypatch):
    """When contacts carry no template, route_template is None."""
    sent = []
    _install_fake_gupshup(monkeypatch, sent)
    _patch_contacts(monkeypatch, ["9111111111"], template=None)
    tmpl_calls = _patch_outcome(monkeypatch, TemplateSendOutcome.NOT_CONFIGURED)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    await asend_escalation_notification(
        "body", client_id="c1", category="Return Request", details="summary text"
    )
    assert tmpl_calls[0]["route_template"] is None


async def test_route_template_uses_global_param_order(monkeypatch):
    """``amaybe_send_escalation_template`` uses route_template's template_id
    and inherits param_order from the global ``escalation_template`` config."""
    http = _FakeHTTP(status_code=200)
    _patch_http(monkeypatch, http)
    _patch_config(monkeypatch, {
        "escalation_template": {"param_order": ["category", "order_id"]},
        "gupshup_template_details": _LEGACY_CREDS,
    })

    route_tmpl = {
        "template_id": "route_specific_tmpl",
        "image_url": None,
    }
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template=route_tmpl,
    )
    assert outcome == TemplateSendOutcome.SENT
    tmpl = _decode_template(http.calls[0])
    assert tmpl["id"] == "route_specific_tmpl"
    assert tmpl["params"] == ["Return Request", "1001"]


async def test_route_template_inherits_param_order_from_global(monkeypatch):
    """param_order is always read from the global ``escalation_template``
    config, even when route_template provides the template_id."""
    http = _FakeHTTP(status_code=200)
    _patch_http(monkeypatch, http)
    _patch_config(monkeypatch, {
        "escalation_template": {
            "param_order": ["priority", "category", "customer_contact", "order_id", "summary"],
        },
        "gupshup_template_details": _LEGACY_CREDS,
    })

    route_tmpl = {"template_id": "route_tmpl_xyz"}
    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template=route_tmpl,
    )
    assert outcome == TemplateSendOutcome.SENT
    tmpl = _decode_template(http.calls[0])
    assert tmpl["id"] == "route_tmpl_xyz"
    assert tmpl["params"] == ["", "Return Request", "9876543210", "1001", "Customer wants to return item"]


async def test_no_route_template_is_not_configured(monkeypatch):
    """When route_template is None, outcome is NOT_CONFIGURED (no global
    fallback for template_id)."""
    _patch_config(monkeypatch, {
        "escalation_template": {"param_order": ["category"]},
        "gupshup_template_details": _LEGACY_CREDS,
    })

    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template=None,
    )
    assert outcome == TemplateSendOutcome.NOT_CONFIGURED


async def test_empty_route_template_is_not_configured(monkeypatch):
    """A route_template dict without template_id → NOT_CONFIGURED."""
    _patch_config(monkeypatch, {
        "escalation_template": {"param_order": ["category"]},
        "gupshup_template_details": _LEGACY_CREDS,
    })

    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={},
    )
    assert outcome == TemplateSendOutcome.NOT_CONFIGURED


async def test_route_template_static_when_no_global_config(monkeypatch):
    """When route_template has a template_id but global config is missing,
    param_order defaults to empty (static template)."""
    http = _FakeHTTP(status_code=200)
    _patch_http(monkeypatch, http)
    _patch_config(monkeypatch, {
        "gupshup_template_details": _LEGACY_CREDS,
    })

    outcome = await amaybe_send_escalation_template(
        to="9111111111", client_id="c1", template_fields=_FIELDS,
        route_template={"template_id": "route_tmpl_static"},
    )
    assert outcome == TemplateSendOutcome.SENT
    tmpl = _decode_template(http.calls[0])
    assert tmpl["id"] == "route_tmpl_static"
    assert tmpl["params"] == []
