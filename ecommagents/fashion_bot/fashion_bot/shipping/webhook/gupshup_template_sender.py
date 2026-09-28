"""
Vendor-agnostic Gupshup WhatsApp template sender for shipping webhooks.

Originally lived under ``fashion_bot/shiprocket/webhook/`` and was named
``asend_shiprocket_gupshup_template_generic``. The body is partner-neutral —
it just posts to Gupshup with whatever ``template_id`` + ``params`` the
caller supplies — so we moved it here and made the per-partner log tag a
parameter. The old name remains as a back-compat re-export so external
callers keep working until they migrate.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, unquote

from fashion_bot.utils.utils import logger as _default_logger
from fashion_bot.utils.http_client import get_shared_async_http_client


async def aget_gupshup_config(client_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load per-tenant Gupshup template configuration from ``client_configs``."""
    try:
        from fashion_bot.config_manager import aget_config

        gupshup_details_json = await aget_config(
            "gupshup_template_details", client_id=client_id
        )
        if gupshup_details_json:
            if isinstance(gupshup_details_json, str):
                gupshup_config = json.loads(gupshup_details_json)
            else:
                gupshup_config = gupshup_details_json
            if gupshup_config and isinstance(gupshup_config, dict):
                _default_logger.debug(
                    f"✅ Gupshup template config loaded from DB for client_id={client_id}"
                )
                return gupshup_config

        _default_logger.warning(
            f"⚠️ No gupshup_template_details found in client_configs for "
            f"client_id={client_id}, using fallback configuration"
        )
        return None

    except Exception as exc:
        _default_logger.error(
            f"❌ Error fetching async Gupshup configuration from DB for "
            f"client_id={client_id}: {exc}"
        )
        return None


# Hardcoded fallback (only used when no DB config is present).
GUPSHUP_API_KEY = ""
GUPSHUP_SOURCE = ""
GUPSHUP_URL = "https://api.gupshup.io/wa/api/v1/template/msg"
APP_NAME = "Ecommagents"


def normalize_phone(phone: Any) -> str:
    """Normalise a raw phone number to international (+91…) format."""
    phone = str(phone or "")
    phone = re.sub(r"[^\d+]", "", phone)
    phone = phone.lstrip("0")
    if phone.startswith("+91"):
        pass
    elif phone.startswith("91") and len(phone) == 12:
        phone = "+" + phone
    elif len(phone) == 10:
        phone = "+91" + phone
    elif not phone.startswith("+"):
        phone = "+" + phone
    return phone


async def asend_gupshup_template_generic(
    destination_phone: Any,
    template_id: str,
    params: List[str],
    image_url: Optional[str] = None,
    client_id: Optional[str] = None,
    event_key: Optional[str] = None,
    template_name: Optional[str] = None,
    *,
    trace_id: Optional[str] = None,
    log_tag: str = "SHIPPING",
) -> Optional[Dict[str, Any]]:
    """Async Gupshup WhatsApp template sender for request-path webhook delivery.

    ``log_tag`` is purely cosmetic — it identifies which partner triggered
    this send in log lines (``[SHIPROCKET]``, ``[DELHIVERY]``, …). The
    function itself is partner-neutral.
    """
    # Multi-version routing: resolve the transport once. Enterprise tenants use
    # the new Gupshup GatewayAPI; everyone else uses the legacy v1 transport.
    # Both branches share the SAME post-send side effects (delivery logging +
    # conversation history) below — only the wire transport differs, so neither
    # version loses observability or conversation continuity.
    from fashion_bot.utils.whatsapp_api_version import (
        WHATSAPP_API_VERSION_ENTERPRISE,
        aget_whatsapp_api_version,
    )

    is_enterprise = (
        await aget_whatsapp_api_version(client_id, trace_id)
        == WHATSAPP_API_VERSION_ENTERPRISE
    )

    logger = logging.getLogger(__name__)
    phone = normalize_phone(destination_phone)
    template_message: Optional[str] = None
    template_params_dict: Optional[Dict[str, Any]] = None
    response_data: Optional[Dict[str, Any]] = None
    success = False
    error_message: Optional[str] = None

    logger.info(f"[{log_tag}] 📤 Sending WhatsApp template message to {phone}")
    if not is_enterprise:
        # Legacy v1 can fetch the template body for a human-readable preview.
        # Enterprise tenants use a different template API, so avoid this call
        # to prevent noisy 400s and keep template_message/template_params null.
        try:
            from fashion_bot.utils.gupshup_api_client import (
                afetch_template_from_gupshup,
                render_template_message,
            )

            template_def = await afetch_template_from_gupshup(template_id, client_id=client_id)
            if template_def and params:
                template_message = render_template_message(template_def, params)
                if template_message:
                    template_params_dict = {
                        "params": params,
                        "param_count": len(params),
                        "image_url": image_url,
                    }
                    logger.info(f"[{log_tag}] ✅ Rendered template preview: {template_message}")
        except Exception as render_error:
            logger.warning(f"[{log_tag}] ⚠️  Error fetching/rendering template: {render_error}")
    else:
        logger.debug(
            "[%s] skipping legacy template preview fetch for enterprise client_id=%s template_id=%s",
            log_tag,
            client_id,
            template_id,
        )

    if is_enterprise:
        # New transport: credentials are loaded tenant-scoped from the tiered
        # cache inside the enterprise client.
        from fashion_bot.utils.whatsapp_enterprise_client import asend_enterprise_template

        effective_image_url = image_url
        if not effective_image_url and client_id and event_key:
            try:
                from fashion_bot.shopify.webhook.templates_db import (
                    aget_client_template_image_url_direct,
                )

                candidate_channels = []
                tag_channel = str(log_tag or "").strip().lower()
                if tag_channel and tag_channel != "shipping":
                    candidate_channels.append(tag_channel)
                candidate_channels.extend(["shiprocket", "delhivery", "shopify"])

                seen = set()
                for channel in candidate_channels:
                    if channel in seen:
                        continue
                    seen.add(channel)
                    effective_image_url = await aget_client_template_image_url_direct(
                        client_id,
                        channel,
                        event_key,
                        template_id=template_id,
                    )
                    if effective_image_url:
                        image_url = effective_image_url
                        print(
                            f"[{log_tag}] ENTERPRISE_IMAGE_FROM_DB "
                            f"client_id={client_id} channel={channel} "
                            f"event_key={event_key} image_url={effective_image_url}"
                        )
                        logger.info(
                            "[%s] enterprise image fallback from DB client_id=%s "
                            "channel=%s event_key=%s image_url=%s",
                            log_tag,
                            client_id,
                            channel,
                            event_key,
                            effective_image_url,
                        )
                        break
                if not effective_image_url:
                    print(
                        f"[{log_tag}] ENTERPRISE_IMAGE_FROM_DB_EMPTY "
                        f"client_id={client_id} event_key={event_key}"
                    )
                    logger.warning(
                        "[%s] enterprise image fallback found no DB image "
                        "client_id=%s event_key=%s",
                        log_tag,
                        client_id,
                        event_key,
                    )
            except Exception as img_lookup_error:
                logger.warning(
                    "[%s] enterprise image fallback lookup failed client_id=%s "
                    "event_key=%s err=%s",
                    log_tag,
                    client_id,
                    event_key,
                    img_lookup_error,
                )

        response_data = await asend_enterprise_template(
            destination_phone,
            template_id,
            params,
            image_url=effective_image_url,
            client_id=client_id,
            trace_id=trace_id,
            log_tag=log_tag,
        )
        success = response_data is not None
        if not success:
            error_message = "enterprise gateway send failed"
    else:
        # ---- Legacy Gupshup v1 transport (unchanged behaviour) ----
        gupshup_template_config = await aget_gupshup_config(client_id=client_id)

        if gupshup_template_config:
            gupshup_api_key = gupshup_template_config.get("GUPSHUP_TEMPLATE_API_KEY", "")
            gupshup_source = gupshup_template_config.get("GUPSHUP_TEMPLATE_SOURCE", "")
            gupshup_url = gupshup_template_config.get(
                "GUPSHUP_TEMPLATE_URL",
                "https://api.gupshup.io/wa/api/v1/template/msg",
            )
            app_name = gupshup_template_config.get("APP_NAME", "Ecommagents")
        else:
            gupshup_api_key = GUPSHUP_API_KEY
            gupshup_source = GUPSHUP_SOURCE
            gupshup_url = GUPSHUP_URL
            app_name = APP_NAME

        template_str = json.dumps({"id": template_id, "params": params or []})
        image_url_norm = unquote(image_url) if image_url else None
        message_str = (
            json.dumps({"image": {"link": image_url_norm}, "type": "image"})
            if image_url_norm else None
        )

        payload = {
            "channel": "whatsapp",
            "source": gupshup_source,
            "destination": phone,
            "src.name": app_name,
            "template": template_str,
        }
        if message_str:
            payload["message"] = message_str

        headers = {
            "apikey": gupshup_api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            import time as _t
            client = await get_shared_async_http_client()
            _t0 = _t.monotonic()
            response = await client.post(
                gupshup_url,
                data=urlencode(payload),
                headers=headers,
                timeout=10,
            )
            logger.info(
                f"[GUPSHUP] POST template elapsed_ms="
                f"{int((_t.monotonic() - _t0) * 1000)} status={response.status_code} "
                f"client_id={client_id or 'unknown'}"
            )
            if response.status_code in [200, 202]:
                success = True
                response_data = response.json()
            else:
                error_message = f"HTTP {response.status_code}"
                if response.status_code in (401, 403):
                    logger.error(
                        f"[GUPSHUP] Authentication failure while sending template "
                        f"client_id={client_id or 'unknown'} status={response.status_code}"
                    )
        except Exception as exc:
            logger.error(
                f"[GUPSHUP] Error sending template for client_id={client_id or 'unknown'}: {exc}"
            )
            error_message = str(exc)

    # ---- Shared post-send side effects (BOTH transports) ----
    if client_id:
        try:
            from fashion_bot.utils.template_delivery_logger import alog_template_delivery

            message_id = None
            if isinstance(response_data, dict):
                message_id = (
                    response_data.get("messageId")
                    or response_data.get("id")
                    # enterprise GatewayAPI shape: {"response": {"id": ...}}
                    or (response_data.get("response") or {}).get("id")
                )
            await alog_template_delivery(
                client_id=client_id,
                phone_number=phone,
                template_id=template_id,
                success=success,
                event_key=event_key,
                channel="whatsapp",
                response_data=response_data,
                error_message=error_message,
                template_name=template_name,
                template_message=template_message,
                template_params=template_params_dict,
                message_id=message_id,
            )
        except Exception as log_error:
            logger.warning(f"Failed to log template delivery: {log_error}")

    if success and template_message and client_id:
        try:
            from fashion_bot.utils.conversation_message_adder import (
                aadd_template_to_conversation_safe,
            )

            conv_id = await aadd_template_to_conversation_safe(
                client_id=client_id,
                phone_number=phone,
                template_message=template_message,
                channel_type="whatsapp",
                template_id=template_id,
                template_name=template_name,
            )
            if conv_id:
                logger.info(f"[{log_tag}] ✅ Template-initiated conversation created: {conv_id}")
            else:
                logger.warning(f"[{log_tag}] ⚠️  Could not add template message to conversation")
        except Exception as conv_error:
            logger.warning(
                f"[{log_tag}] ⚠️  Error adding template to conversation: {conv_error}"
            )

    return response_data if success else None
