"""Gupshup *enterprise* WhatsApp GatewayAPI client.

This is the "new version" transport selected by
:func:`fashion_bot.utils.whatsapp_api_version.aget_whatsapp_api_version`. It is
the function the existing send paths delegate to when a tenant is configured for
``whatsapp_api_version = enterprise`` — the legacy code is left untouched.

Template transport:

    GET https://mediaapi.smsgupshup.com/GatewayAPI/rest
    query: method=SendMediaMessage&userid=<client id>&password=<password>&...

Credentials (``userid`` + ``password``) are tenant-scoped and loaded through the
three-tier cache (memory → Redis → DB, AGENTS.md §3) — never hardcoded (spec §0,
AGENTS.md §7). Template sends use ``gupshup_enterprise_details``; free-text
session sends use ``gupshup_enterprise_details_text``.

Scope (deliberate subset of the spec's 23 variants):
    * Implemented: plain **text template** (variant 1) and **media/image
      template** (variants 9/12) via ``build_template_params``, plus a
      free-text **session** message via ``build_session_text_params`` (the
      latter is not in the spec's template catalog but the live gateway accepts
      the ``msg`` shape inside an open 24h window — verified, not spec-derived).
    * NOT implemented: button/QR/CTA variants, ``wa_template_json`` payloads
      (coupon/LTO/carousel/flows), location, catalog, MPM/single-product, and
      the per-variant ``linkTrackingEnabled`` / ``msg_id`` params. Callers
      needing those must extend this module with typed per-variant helpers
      (spec §4) — today such params are simply not emitted, never silently
      mis-sent for the supported text/image paths.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import unquote, urlencode

from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.template_param_resolver import resolve_template_params

logger = logging.getLogger(__name__)

# Config key in ``client_configs`` holding enterprise text/session credentials.
GUPSHUP_ENTERPRISE_TEXT_CONFIG_KEY = "gupshup_enterprise_details_text"
# Config key in ``client_configs`` holding enterprise template credentials.
GUPSHUP_ENTERPRISE_TEMPLATE_CONFIG_KEY = "gupshup_enterprise_details"

DEFAULT_GATEWAY_URL = "https://mediaapi.smsgupshup.com/GatewayAPI/rest"

# Canonical method casing (spec §2: the gateway is case-insensitive; we pick one).
METHOD_SEND_MESSAGE = "SendMessage"
METHOD_SEND_MEDIA_MESSAGE = "SendMediaMessage"

# Base params present on (almost) every request (spec §1).
_BASE_PARAMS = {"auth_scheme": "plain", "v": "1.1", "format": "json"}


def normalize_msisdn(phone: Any) -> str:
    """Normalise a raw phone number to a bare MSISDN (e.g. ``919876543210``).

    The GatewayAPI expects ``send_to`` without a leading ``+``. A bare 10-digit
    Indian number is prefixed with the ``91`` country code.
    """
    digits = re.sub(r"\D", "", str(phone or "")).lstrip("0")
    if len(digits) == 10:
        digits = f"91{digits}"
    return digits


async def _aget_enterprise_config_by_key(
    client_id: Optional[str],
    config_key: str,
) -> Optional[Dict[str, Any]]:
    """Load tenant-scoped enterprise credentials via the three-tier cache.

    Returns ``None`` when no ``client_id`` is supplied or no config row exists
    (callers must treat this as "not configured" — never fall back to another
    tenant's credentials, AGENTS.md §7).
    """
    if not client_id:
        return None
    try:
        from fashion_bot.config_manager import aget_config

        raw = await aget_config(config_key, client_id=client_id)
        if not raw:
            return None
        config = json.loads(raw) if isinstance(raw, str) else raw
        return config if isinstance(config, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[ENTERPRISE_WA] failed loading config for client_id=%s err=%s",
            client_id,
            exc,
        )
        return None


async def aget_enterprise_config(
    client_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Load enterprise template credentials from ``gupshup_enterprise_details``."""
    return await _aget_enterprise_config_by_key(
        client_id,
        GUPSHUP_ENTERPRISE_TEMPLATE_CONFIG_KEY,
    )


async def aget_enterprise_text_config(
    client_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Load enterprise text/session credentials from ``gupshup_enterprise_details_text``."""
    return await _aget_enterprise_config_by_key(
        client_id,
        GUPSHUP_ENTERPRISE_TEXT_CONFIG_KEY,
    )


def _resolve_credentials(config: Dict[str, Any]) -> Dict[str, str]:
    """Extract userid / password / url from a config dict, tolerating aliases."""
    userid = (
        config.get("GUPSHUP_ENTERPRISE_USERID")
        or config.get("GUPSHUP_ENTERPRISE_USER_ID")
        or config.get("userId")
        or config.get("USERID")
        or config.get("userid")
        or config.get("client_id")
        or ""
    )
    password = (
        config.get("GUPSHUP_ENTERPRISE_PASSWORD")
        or config.get("password")
        or config.get("PASSWORD")
        # Back-compat for tenants seeded while this module used bearer-token
        # terminology. Template sends still place the value in password=...
        or config.get("GUPSHUP_ENTERPRISE_TOKEN")
        or config.get("token")
        or config.get("secret")
        or ""
    )
    url = (
        config.get("GUPSHUP_ENTERPRISE_URL")
        or config.get("url")
        or DEFAULT_GATEWAY_URL
    )
    return {"userid": str(userid), "password": str(password), "url": str(url)}


def build_template_params(
    *,
    userid: str,
    password: str,
    send_to: str,
    template_id: str,
    params: Optional[Union[Sequence[Any], Mapping[str, Any]]] = None,
    param_order: Optional[Sequence[Any]] = None,
    image_url: Optional[str] = None,
    msg_id: Optional[str] = None,
) -> Dict[str, str]:
    """Assemble the GatewayAPI form body for a template (HSM) send.

    Maps the bot's ``params`` list onto ``var1..varN`` and creates the media
    template query shape required by the enterprise GatewayAPI.
    """
    if not image_url:
        raise ValueError("enterprise template send requires image_url")
    body: Dict[str, str] = dict(_BASE_PARAMS)
    body["userid"] = userid
    body["password"] = password
    body["send_to"] = send_to
    body["whatsAppTemplateId"] = template_id
    body["isHSM"] = "true"
    body["isTemplate"] = "false"
    body["method"] = METHOD_SEND_MEDIA_MESSAGE
    body["msg_type"] = "IMAGE"
    # Normalize first so httpx encodes exactly once, regardless of whether the
    # caller passed an already percent-encoded URL (avoids %20 -> %2520).
    body["media_url"] = unquote(image_url)
    for idx, value in enumerate(resolve_template_params(params, param_order), start=1):
        body[f"var{idx}"] = str(value)
    if msg_id:
        body["msg_id"] = msg_id
    return body


def build_session_text_params(
    *, userid: str, password: str, send_to: str, message: str
) -> Dict[str, str]:
    """Assemble the GatewayAPI form body for a free-text (session) reply.

    Not a template send — ``isHSM`` is omitted (``msg_type=TEXT`` + ``msg``).
    """
    body: Dict[str, str] = dict(_BASE_PARAMS)
    body["method"] = METHOD_SEND_MESSAGE
    body["userid"] = userid
    body["password"] = password
    body["send_to"] = send_to
    body["msg_type"] = "TEXT"
    body["msg"] = message
    return body


# Default wire values for a body-only (no media) HSM on the GatewayAPI. Gupshup
# enterprise docs describe a text template as ``method=SendMessage`` +
# ``isHSM=true`` (``msg_type=HSM``); the ``whatsAppTemplateId`` + ``var1..varN``
# shape is the same one the (verified) media path uses, minus the media header.
# Both are overridable per-tenant (``gupshup_enterprise_details`` keys
# ``text_hsm_method`` / ``text_hsm_msg_type``) so the exact casing can be tuned
# against a live gateway without a redeploy.
_TEXT_HSM_DEFAULT_METHOD = METHOD_SEND_MESSAGE
_TEXT_HSM_DEFAULT_MSG_TYPE = "HSM"


def build_text_template_params(
    *,
    userid: str,
    password: str,
    send_to: str,
    template_id: str,
    params: Optional[Union[Sequence[Any], Mapping[str, Any]]] = None,
    param_order: Optional[Sequence[Any]] = None,
    method: str = _TEXT_HSM_DEFAULT_METHOD,
    msg_type: str = _TEXT_HSM_DEFAULT_MSG_TYPE,
    msg_id: Optional[str] = None,
) -> Dict[str, str]:
    """Assemble the GatewayAPI form body for a **text-only** (no media) HSM send.

    Mirrors :func:`build_template_params` (``whatsAppTemplateId`` + ``var1..varN``,
    ``isHSM=true``) but emits **no** ``media_url`` and uses ``method=SendMessage``
    / ``msg_type=HSM`` instead of the media pair. Unlike the media builder this
    does not require an ``image_url`` — escalation templates are typically
    text/body-only.
    """
    body: Dict[str, str] = dict(_BASE_PARAMS)
    body["userid"] = userid
    body["password"] = password
    body["send_to"] = send_to
    body["whatsAppTemplateId"] = template_id
    body["isHSM"] = "true"
    body["isTemplate"] = "false"
    body["method"] = method or _TEXT_HSM_DEFAULT_METHOD
    body["msg_type"] = msg_type or _TEXT_HSM_DEFAULT_MSG_TYPE
    for idx, value in enumerate(resolve_template_params(params, param_order), start=1):
        body[f"var{idx}"] = str(value)
    if msg_id:
        body["msg_id"] = msg_id
    return body


def _is_success_response(data: Any) -> bool:
    """True when ``data`` matches the gateway's standard success shape (spec §1)."""
    if not isinstance(data, dict):
        return False
    resp = data.get("response")
    if not isinstance(resp, dict):
        return False
    return str(resp.get("status", "")).lower() == "success" and bool(resp.get("id"))


async def _apost_gateway(
    *,
    url: str,
    body: Dict[str, str],
    client_id: Optional[str],
    trace_id: Optional[str],
    log_tag: str,
) -> Optional[Dict[str, Any]]:
    """POST a form body to the GatewayAPI and return parsed JSON on success."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        client = await get_shared_async_http_client()
        t0 = time.monotonic()
        response = await client.post(
            url, data=urlencode(body), headers=headers, timeout=30
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if response.status_code in (200, 202):
            data = response.json()
            ok = _is_success_response(data)
            logger.info(
                "[%s] enterprise POST method=%s elapsed_ms=%s status=%s success=%s "
                "client_id=%s trace_id=%s",
                log_tag,
                body.get("method"),
                elapsed_ms,
                response.status_code,
                ok,
                client_id or "unknown",
                trace_id,
            )
            if not ok:
                # Capture the gateway's error detail (spec §5) for diagnosis.
                logger.error(
                    "[%s] enterprise non-success body=%s client_id=%s trace_id=%s",
                    log_tag,
                    data,
                    client_id or "unknown",
                    trace_id,
                )
                return None
            return data
        logger.error(
            "[%s] enterprise POST failed status=%s body=%s client_id=%s trace_id=%s",
            log_tag,
            response.status_code,
            response.text[:500],
            client_id or "unknown",
            trace_id,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - graceful degradation (AGENTS.md)
        logger.error(
            "[%s] enterprise POST exception client_id=%s trace_id=%s err_type=%s err=%r",
            log_tag,
            client_id or "unknown",
            trace_id,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return None


async def _aget_gateway(
    *,
    url: str,
    params: Dict[str, str],
    client_id: Optional[str],
    trace_id: Optional[str],
    log_tag: str,
) -> Optional[Dict[str, Any]]:
    """GET the GatewayAPI with query params and return parsed JSON on success."""
    debug_params = dict(params)
    if debug_params.get("password"):
        debug_params["password"] = "***"
    # Mask template variable values — they can carry customer PII (name/contact
    # and, for escalation templates, the conversation summary). The masking is
    # log-only; the actual request still carries the real values.
    for _k in list(debug_params.keys()):
        if _k.startswith("var"):
            debug_params[_k] = "***"
    debug_url = f"{url}?{urlencode(debug_params)}"
    print(f"[{log_tag}] ENTERPRISE_GUPSHUP_GET {debug_url}")
    logger.info(
        "[%s] enterprise GET url=%s client_id=%s trace_id=%s",
        log_tag,
        debug_url,
        client_id or "unknown",
        trace_id,
    )
    try:
        client = await get_shared_async_http_client()
        t0 = time.monotonic()
        response = await client.get(url, params=params, timeout=30)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if response.status_code in (200, 202):
            data = response.json()
            ok = _is_success_response(data)
            logger.info(
                "[%s] enterprise GET method=%s elapsed_ms=%s status=%s success=%s "
                "client_id=%s trace_id=%s",
                log_tag,
                params.get("method"),
                elapsed_ms,
                response.status_code,
                ok,
                client_id or "unknown",
                trace_id,
            )
            if not ok:
                logger.error(
                    "[%s] enterprise GET non-success body=%s client_id=%s trace_id=%s",
                    log_tag,
                    data,
                    client_id or "unknown",
                    trace_id,
                )
                return None
            return data
        logger.error(
            "[%s] enterprise GET failed status=%s body=%s client_id=%s trace_id=%s",
            log_tag,
            response.status_code,
            response.text[:500],
            client_id or "unknown",
            trace_id,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - graceful degradation (AGENTS.md)
        logger.error(
            "[%s] enterprise GET exception client_id=%s trace_id=%s err=%s",
            log_tag,
            client_id or "unknown",
            trace_id,
            exc,
        )
        return None


async def asend_enterprise_template(
    destination_phone: Any,
    template_id: str,
    params: Optional[Union[Sequence[Any], Mapping[str, Any]]] = None,
    image_url: Optional[str] = None,
    client_id: Optional[str] = None,
    *,
    param_order: Optional[Sequence[Any]] = None,
    msg_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    log_tag: str = "ENTERPRISE_WA",
) -> Optional[Dict[str, Any]]:
    """Send a WhatsApp template via the enterprise GatewayAPI.

    Credentials are loaded tenant-scoped through the three-tier cache.
    Returns the gateway JSON on success, ``None`` otherwise.
    """
    config = await aget_enterprise_config(client_id)
    if not config:
        logger.error(
            "[%s] no enterprise config for client_id=%s; cannot send template "
            "trace_id=%s",
            log_tag,
            client_id or "unknown",
            trace_id,
        )
        return None
    creds = _resolve_credentials(config)
    if not creds["userid"] or not creds["password"]:
        logger.error(
            "[%s] incomplete enterprise credentials for client_id=%s trace_id=%s",
            log_tag,
            client_id or "unknown",
            trace_id,
        )
        return None
    try:
        body = build_template_params(
            userid=creds["userid"],
            password=creds["password"],
            send_to=normalize_msisdn(destination_phone),
            template_id=template_id,
            params=params,
            param_order=param_order,
            image_url=image_url,
            msg_id=msg_id,
        )
    except ValueError as exc:
        logger.error(
            "[%s] enterprise template param resolution failed client_id=%s "
            "trace_id=%s err=%s",
            log_tag,
            client_id or "unknown",
            trace_id,
            exc,
        )
        return None
    return await _aget_gateway(
        url=creds["url"],
        params=body,
        client_id=client_id,
        trace_id=trace_id,
        log_tag=log_tag,
    )


async def asend_enterprise_text_template(
    destination_phone: Any,
    template_id: str,
    params: Optional[Union[Sequence[Any], Mapping[str, Any]]] = None,
    client_id: Optional[str] = None,
    *,
    param_order: Optional[Sequence[Any]] = None,
    msg_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    log_tag: str = "ENTERPRISE_WA",
) -> Optional[Dict[str, Any]]:
    """Send a **text-only** (no media) WhatsApp HSM via the enterprise GatewayAPI.

    Uses the template credentials (``gupshup_enterprise_details``) — same as the
    media template send — because an HSM authenticates against the template app,
    not the free-text session app. Posts (rather than GETs) so the customer
    values in ``var1..varN`` are not placed in the request URL. Returns the
    gateway JSON on success, ``None`` otherwise.

    NOTE: the exact ``method``/``msg_type`` for a body-only HSM on this gateway
    are taken from Gupshup enterprise docs (``SendMessage`` / ``HSM``) and are
    overridable per-tenant; confirm against the live gateway (see
    ``tests/test_whatsapp_api_versioning.py::test_live_send_to_gant...``) before
    relying on it in production. The send is fail-open — an unsupported shape
    returns ``None`` and the caller degrades gracefully.
    """
    config = await aget_enterprise_config(client_id)
    if not config:
        logger.error(
            "[%s] no enterprise config for client_id=%s; cannot send text template "
            "trace_id=%s",
            log_tag,
            client_id or "unknown",
            trace_id,
        )
        return None
    creds = _resolve_credentials(config)
    if not creds["userid"] or not creds["password"]:
        logger.error(
            "[%s] incomplete enterprise credentials for client_id=%s trace_id=%s",
            log_tag,
            client_id or "unknown",
            trace_id,
        )
        return None
    body = build_text_template_params(
        userid=creds["userid"],
        password=creds["password"],
        send_to=normalize_msisdn(destination_phone),
        template_id=template_id,
        params=params,
        param_order=param_order,
        method=str(config.get("text_hsm_method") or _TEXT_HSM_DEFAULT_METHOD),
        msg_type=str(config.get("text_hsm_msg_type") or _TEXT_HSM_DEFAULT_MSG_TYPE),
        msg_id=msg_id,
    )
    return await _apost_gateway(
        url=creds["url"],
        body=body,
        client_id=client_id,
        trace_id=trace_id,
        log_tag=log_tag,
    )


async def asend_enterprise_text_message(
    to: Any,
    message: str,
    client_id: Optional[str] = None,
    *,
    trace_id: Optional[str] = None,
    log_tag: str = "ENTERPRISE_WA",
) -> Optional[Dict[str, Any]]:
    """Send a free-text (session) reply via the enterprise GatewayAPI."""
    config = await aget_enterprise_text_config(client_id)
    if not config:
        logger.error(
            "[%s] no enterprise config for client_id=%s; cannot send text "
            "trace_id=%s",
            log_tag,
            client_id or "unknown",
            trace_id,
        )
        return None
    creds = _resolve_credentials(config)
    if not creds["userid"] or not creds["password"]:
        logger.error(
            "[%s] incomplete enterprise credentials for client_id=%s trace_id=%s",
            log_tag,
            client_id or "unknown",
            trace_id,
        )
        return None
    body = build_session_text_params(
        userid=creds["userid"],
        password=creds["password"],
        send_to=normalize_msisdn(to),
        message=message,
    )
    return await _apost_gateway(
        url=creds["url"],
        body=body,
        client_id=client_id,
        trace_id=trace_id,
        log_tag=log_tag,
    )
