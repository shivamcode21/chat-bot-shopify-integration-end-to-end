"""Opt-in delivery of **staff** escalation alerts via Gupshup templates.

Escalations are normally delivered to staff as free-text WhatsApp "session"
messages (``fashion_bot.gupshup_webhook.send_message``). WhatsApp only delivers
a free-text message inside an open 24-hour customer-service window, so a staff
recipient whose window is closed silently misses the alert. Business-initiated
messaging outside that window requires a pre-approved template (HSM).

This module lets a client **opt in** (via the ``escalation_template`` client
config) to have staff escalation alerts sent through such a template. It is a
deliberate, self-contained path — it does **not** reuse
``asend_shopify_gupshup_template_generic`` because that helper writes customer
``template_delivery`` analytics rows and creates ``template_initiated``
*customer* conversations keyed on the recipient's phone number
(``shopify/webhook/gupshup_template_sender.py``); doing that for a staff number
would pollute customer-facing data. The raw sender here performs the gateway
POST only.

**Transports.** Both are supported and selected per-client by
``whatsapp_api_version``:

* **legacy** — raw POST to ``api.gupshup.io/wa/api/v1/template/msg`` (here).
* **enterprise** — the GatewayAPI (``mediaapi.smsgupshup.com``) via the verified
  media path (``asend_enterprise_template``) when the template has an image
  header, otherwise the text-only HSM path (``asend_enterprise_text_template``).
  The text-HSM wire format mirrors the proven media path minus the media header;
  its exact ``method``/``msg_type`` are overridable per-tenant and should be
  confirmed against the live gateway (see the ``gant`` live integration test)
  before production reliance. All sends are fail-open.

See ``design_docs/ESCALATION_GUPSHUP_TEMPLATES.md``.
"""
from __future__ import annotations

import json
import logging
import re
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlencode

from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)

# Client-config key holding the opt-in escalation template settings.
ESCALATION_TEMPLATE_CONFIG_KEY = "escalation_template"

_DEFAULT_LEGACY_TEMPLATE_URL = "https://api.gupshup.io/wa/api/v1/template/msg"

# Values a client might store for the ``enabled`` flag.
_TRUTHY = {"1", "true", "yes", "on", "enabled"}


class TemplateSendOutcome(str, Enum):
    """Result of an escalation-template send attempt.

    The distinction between *pre-gateway* and *post-gateway* failure is what
    lets the caller avoid double-alerting: we only fall back to a free-text send
    when the gateway was **never hit** (``NOT_CONFIGURED`` / ``FAILED_PRESEND``).
    Once the gateway has accepted or rejected the request we never also send free
    text (``SENT`` / ``GATEWAY_FAILED``).
    """

    NOT_CONFIGURED = "not_configured"   # no/disabled config, or enterprise (phase 1) → free text
    FAILED_PRESEND = "failed_presend"   # deterministic failure before any POST → free text
    SENT = "sent"                       # gateway accepted the template → done
    GATEWAY_FAILED = "gateway_failed"   # gateway rejected/errored → degraded (email covers it)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return False


def _parse_config(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse the stored config, tolerating a JSON string or a dict."""
    if not raw:
        return None
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) else None


async def asend_legacy_gupshup_template_raw(
    *,
    to: str,
    template_id: str,
    params: List[str],
    creds: Dict[str, Any],
    image_url: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """POST a template to the legacy Gupshup ``/template/msg`` endpoint.

    Mirrors the wire shape of ``asend_shopify_gupshup_template_generic`` (legacy
    branch) but performs **only** the gateway POST — no ``template_delivery``
    logging and no conversation creation. Returns the gateway JSON on HTTP
    200/202, else ``None``.
    """
    from fashion_bot.shopify.webhook.gupshup_template_sender import normalize_phone

    phone = normalize_phone(to)
    api_key = creds.get("GUPSHUP_TEMPLATE_API_KEY", "")
    source = creds.get("GUPSHUP_TEMPLATE_SOURCE", "")
    url = creds.get("GUPSHUP_TEMPLATE_URL", _DEFAULT_LEGACY_TEMPLATE_URL)
    app_name = creds.get("APP_NAME", "")

    template_str = json.dumps({"id": template_id, "params": params or []}, separators=(",", ":"))
    image_url_norm = unquote(image_url) if image_url else None
    payload = {
        "channel": "whatsapp",
        "source": source,
        "destination": phone,
        "src.name": app_name,
        "template": template_str,
    }
    if image_url_norm:
        payload["message"] = json.dumps(
            {"image": {"link": image_url_norm}, "type": "image"}, separators=(",", ":")
        )

    headers = {
        "apikey": api_key,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        client = await get_shared_async_http_client()
        response = await client.post(url, data=urlencode(payload), headers=headers, timeout=10)
        if response.status_code in (200, 202):
            logger.info(
                "[ESCALATION_TEMPLATE] legacy send ok status=%s to=***%s trace_id=%s",
                response.status_code,
                phone[-4:],
                trace_id,
            )
            try:
                return response.json()
            except ValueError:
                return {"status": "submitted"}
        logger.error(
            "[ESCALATION_TEMPLATE] legacy send failed status=%s body=%s to=***%s trace_id=%s",
            response.status_code,
            response.text[:300],
            phone[-4:],
            trace_id,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - graceful degradation (AGENTS.md §11)
        logger.error(
            "[ESCALATION_TEMPLATE] legacy send exception to=***%s trace_id=%s err=%s",
            phone[-4:],
            trace_id,
            exc,
        )
        return None


async def amaybe_send_escalation_template(
    *,
    to: str,
    client_id: Optional[str],
    template_fields: Optional[Dict[str, Any]],
    immediate_attention: bool = False,
    trace_id: Optional[str] = None,
    route_template: Optional[Dict[str, Any]] = None,
) -> TemplateSendOutcome:
    """Send the staff escalation alert as a template.

    ``route_template`` carries the template config resolved from the
    ``escalation_contact`` routing nodes (``template_id``, ``image_url``).
    If absent or without a ``template_id``, templates are not configured and
    the caller falls back to free-text.

    ``param_order`` is read from the global ``escalation_template`` config key
    (the only field that key carries). It is shared across all templates for a
    client since all approved templates use the same variable structure.

    Returns a :class:`TemplateSendOutcome`. The caller uses it to decide whether
    to fall back to a free-text send (only for ``NOT_CONFIGURED`` /
    ``FAILED_PRESEND`` — never after the gateway was hit).
    """
    if not client_id or not template_fields:
        return TemplateSendOutcome.NOT_CONFIGURED

    if not route_template or not route_template.get("template_id"):
        return TemplateSendOutcome.NOT_CONFIGURED

    template_id = str(route_template["template_id"])
    image_url = route_template.get("image_url") or None

    # --- Load param_order from global escalation_template config ---
    param_order: list = []
    try:
        from fashion_bot.config_manager import aget_config

        cfg = _parse_config(await aget_config(ESCALATION_TEMPLATE_CONFIG_KEY, client_id=client_id))
        param_order = (cfg or {}).get("param_order") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ESCALATION_TEMPLATE] param_order config read failed client_id=%s trace_id=%s err=%s; "
            "proceeding with empty param_order (static template)",
            client_id, trace_id, exc,
        )

    # --- Resolve transport (pre-gateway; fail open to legacy) ---
    try:
        from fashion_bot.utils.whatsapp_api_version import (
            WHATSAPP_API_VERSION_ENTERPRISE,
            aget_whatsapp_api_version,
        )

        is_enterprise = (
            await aget_whatsapp_api_version(client_id, trace_id)
            == WHATSAPP_API_VERSION_ENTERPRISE
        )
    except Exception as exc:  # noqa: BLE001 - fail open to legacy
        logger.debug("[ESCALATION_TEMPLATE] version resolution failed: %s", exc)
        is_enterprise = False

    # --- Build ordered params (pre-gateway; any error => free-text fallback) ---
    try:
        from fashion_bot.utils.template_param_resolver import resolve_template_params

        # A static-body template (no variables) is valid: empty param_order =>
        # no params. Only derive params from fields when an order is configured.
        params = resolve_template_params(template_fields, param_order) if param_order else []
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[ESCALATION_TEMPLATE] param resolution failed client_id=%s trace_id=%s err=%s; "
            "falling back to free text",
            client_id, trace_id, exc,
        )
        return TemplateSendOutcome.FAILED_PRESEND

    if is_enterprise:
        return await _asend_enterprise(
            to=to,
            client_id=client_id,
            template_id=template_id,
            params=params,
            image_url=image_url,
            trace_id=trace_id,
        )
    return await _asend_legacy(
        to=to,
        client_id=client_id,
        template_id=template_id,
        params=params,
        image_url=image_url,
        trace_id=trace_id,
    )


async def _asend_legacy(
    *,
    to: str,
    client_id: Optional[str],
    template_id: str,
    params: List[str],
    image_url: Optional[str],
    trace_id: Optional[str],
) -> TemplateSendOutcome:
    """Legacy Gupshup ``/template/msg`` send (raw POST, no side effects)."""
    try:
        from fashion_bot.shopify.webhook.gupshup_template_sender import aget_gupshup_config

        creds = await aget_gupshup_config(client_id=client_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[ESCALATION_TEMPLATE] legacy creds load failed client_id=%s trace_id=%s err=%s",
            client_id, trace_id, exc,
        )
        return TemplateSendOutcome.FAILED_PRESEND

    if not creds or not creds.get("GUPSHUP_TEMPLATE_API_KEY"):
        logger.error(
            "[ESCALATION_TEMPLATE] no gupshup_template_details creds for client_id=%s; "
            "falling back to free text",
            client_id,
        )
        return TemplateSendOutcome.FAILED_PRESEND

    response = await asend_legacy_gupshup_template_raw(
        to=to,
        template_id=template_id,
        params=params,
        creds=creds,
        image_url=image_url,
        trace_id=trace_id,
    )
    return TemplateSendOutcome.SENT if response else TemplateSendOutcome.GATEWAY_FAILED


async def _asend_enterprise(
    *,
    to: str,
    client_id: Optional[str],
    template_id: str,
    params: List[str],
    image_url: Optional[str],
    trace_id: Optional[str],
) -> TemplateSendOutcome:
    """Enterprise GatewayAPI send.

    Uses the verified media path when the template has an image header
    (``image_url`` set), otherwise the text-only HSM path. Missing/incomplete
    enterprise credentials are a pre-gateway failure (free-text fallback); a
    gateway rejection is ``GATEWAY_FAILED`` (email is the guaranteed channel).
    """
    try:
        from fashion_bot.utils.whatsapp_enterprise_client import (
            _resolve_credentials,
            aget_enterprise_config,
            asend_enterprise_template,
            asend_enterprise_text_template,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[ESCALATION_TEMPLATE] enterprise client import failed: %s", exc)
        return TemplateSendOutcome.FAILED_PRESEND

    config = await aget_enterprise_config(client_id)
    if not config:
        logger.error(
            "[ESCALATION_TEMPLATE] no gupshup_enterprise_details creds for client_id=%s; "
            "falling back to free text",
            client_id,
        )
        return TemplateSendOutcome.FAILED_PRESEND
    creds = _resolve_credentials(config)
    if not creds.get("userid") or not creds.get("password"):
        logger.error(
            "[ESCALATION_TEMPLATE] incomplete enterprise creds for client_id=%s; "
            "falling back to free text",
            client_id,
        )
        return TemplateSendOutcome.FAILED_PRESEND

    if image_url:
        response = await asend_enterprise_template(
            to,
            template_id,
            params,
            image_url=image_url,
            client_id=client_id,
            trace_id=trace_id,
            log_tag="ESCALATION_TEMPLATE",
        )
    else:
        response = await asend_enterprise_text_template(
            to,
            template_id,
            params,
            client_id=client_id,
            trace_id=trace_id,
            log_tag="ESCALATION_TEMPLATE",
        )
    return TemplateSendOutcome.SENT if response else TemplateSendOutcome.GATEWAY_FAILED


# --------------------------------------------------------------------------- #
# Parameter sanitization
# --------------------------------------------------------------------------- #
# WhatsApp/Gupshup reject template parameters that contain newlines, tabs, or
# runs of 4+ spaces, and reject sends whose parameter is disproportionately long
# relative to the template body. Every value placed in a template param must be
# collapsed to a single line and length-capped.

_HORIZONTAL_WS_RE = re.compile(r"[ \t\f\v]+")
_NEWLINE_RE = re.compile(r"[\r\n]+")
_REPEAT_BULLET_RE = re.compile(r"(?: ?· ?)+")


def sanitize_template_param(value: Any, *, cap: int = 120) -> str:
    """Collapse a value to a single, length-capped line safe for a template param."""
    text = _NEWLINE_RE.sub(" ", str(value if value is not None else ""))
    text = _HORIZONTAL_WS_RE.sub(" ", text)
    text = "".join(ch for ch in text if ch >= " ")  # drop remaining control chars
    text = text.strip()
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
    return text


def sanitize_template_summary(value: Any, *, cap: int = 600) -> str:
    """Flatten a multi-line summary/details block into one capped param line.

    Newlines become ``·`` separators (so structure is hinted at, not lost),
    horizontal whitespace runs collapse to a single space.
    """
    text = _NEWLINE_RE.sub(" · ", str(value if value is not None else ""))
    text = _HORIZONTAL_WS_RE.sub(" ", text)
    text = _REPEAT_BULLET_RE.sub(" · ", text)
    text = "".join(ch for ch in text if ch >= " ")
    text = text.strip(" ·\t")
    if len(text) > cap:
        text = text[: cap - 1].rstrip(" ·") + "…"
    return text
