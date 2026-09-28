import logging
import json
import re
from urllib.parse import urlencode, quote_plus, quote, unquote

from fashion_bot.utils.utils import logger
from fashion_bot.utils.http_client import get_shared_async_http_client


def normalize_phone(phone):
    phone = str(phone)
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

def normalize_image_url(url: str) -> str:
    # Fix double-encoded spaces and ensure single %20 encoding
    url = (url or "").replace("%2520", "%20")
    url = url.replace(" ", "%20")
    return url

def _encode_form_preserving_percent(payload: dict) -> str:
    parts = []
    for k, v in payload.items():
        key = quote_plus(str(k))
        value = str(v)
        if k in ("template", "message"):
            encoded_value = quote(value, safe='{}":,[]/% ')
        else:
            encoded_value = quote_plus(value)
        parts.append(f"{key}={encoded_value}")
    return "&".join(parts)


async def aget_gupshup_config(client_id=None):
    """Async variant of get_gupshup_config."""
    try:
        from fashion_bot.config_manager import aget_config
        import json

        # Fetch gupshup_template_details scoped to the client
        gupshup_details_json = await aget_config('gupshup_template_details', client_id=client_id)

        if gupshup_details_json:
            # Parse JSON if it's a string, otherwise use as-is if already dict
            if isinstance(gupshup_details_json, str):
                gupshup_config = json.loads(gupshup_details_json)
            else:
                gupshup_config = gupshup_details_json

            if gupshup_config and isinstance(gupshup_config, dict):
                logger.debug(f"✅ Gupshup template config loaded from database for client_id={client_id}")
                return gupshup_config

        logger.warning(f"⚠️ No gupshup_template_details found in client_configs for client_id={client_id}, using fallback configuration")
        return None

    except Exception as e:
        logger.error(f"❌ Error fetching async Gupshup configuration from database for client_id={client_id}: {str(e)}")
        return None


# Fallback configuration (only used when database config unavailable)
GUPSHUP_API_KEY = ""
GUPSHUP_SOURCE = ""
GUPSHUP_URL = "https://api.gupshup.io/wa/api/v1/template/msg"
APP_NAME = "Ecommagents"

async def asend_shopify_gupshup_template_generic(
    destination_phone,
    template_id,
    params,
    image_url=None,
    client_id=None,
    event_key=None,
    template_name=None,
    order_id=None,
    log_tag=None,
):
    """
    Async Shopify WhatsApp template sender for webhook delivery paths.
    """
    logger = logging.getLogger(__name__)
    phone = normalize_phone(destination_phone)
    template_message = None
    template_params_dict = None
    response_data = None
    success = False
    error_message = None
    effective_log_tag = str(log_tag or template_name or event_key or "SHOPIFY")

    from fashion_bot.utils.whatsapp_api_version import (
        WHATSAPP_API_VERSION_ENTERPRISE,
        aget_whatsapp_api_version,
    )

    is_enterprise = (
        await aget_whatsapp_api_version(client_id)
        == WHATSAPP_API_VERSION_ENTERPRISE
    )

    if not is_enterprise:
        try:
            from fashion_bot.utils.gupshup_api_client import afetch_template_from_gupshup, render_template_message

            template_def = await afetch_template_from_gupshup(template_id, client_id=client_id)
            if template_def and params:
                template_message = render_template_message(template_def, params)
                if template_message:
                    template_params_dict = {
                        "params": params,
                        "param_count": len(params),
                        "image_url": image_url,
                    }
                    logger.info(f"[TEMPLATE_RENDER] Rendered message for order {order_id or 'N/A'}: {template_message}")
        except Exception as render_error:
            logger.warning(f"[TEMPLATE_FETCH] Error: {render_error}")
    else:
        logger.debug(
            "[TEMPLATE_FETCH] Skipping legacy template preview fetch for enterprise client_id=%s template_id=%s",
            client_id,
            template_id,
        )

    if is_enterprise:
        from fashion_bot.utils.whatsapp_enterprise_client import asend_enterprise_template

        effective_image_url = image_url
        if not effective_image_url and client_id and event_key:
            try:
                from fashion_bot.shopify.webhook.templates_db import (
                    aget_client_template_image_url_direct,
                )

                effective_image_url = await aget_client_template_image_url_direct(
                    client_id,
                    "shopify",
                    event_key,
                    template_id=template_id,
                )
                image_url = effective_image_url
                print(
                    f"[{effective_log_tag}] ENTERPRISE_IMAGE_FROM_DB "
                    f"client_id={client_id} event_key={event_key} "
                    f"image_url={effective_image_url or '(empty)'}"
                )
                logger.info(
                    "[%s] enterprise image fallback from DB client_id=%s "
                    "event_key=%s image_url=%s",
                    effective_log_tag,
                    client_id,
                    event_key,
                    effective_image_url or "N/A",
                )
            except Exception as img_lookup_error:
                logger.warning(
                    "[%s] enterprise image fallback lookup failed client_id=%s "
                    "event_key=%s err=%s",
                    effective_log_tag,
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
            log_tag=effective_log_tag,
        )
        success = response_data is not None
        if not success:
            error_message = "enterprise gateway send failed"
    else:
        _gupshup_template_config = await aget_gupshup_config(client_id=client_id)

        if _gupshup_template_config:
            gupshup_api_key = _gupshup_template_config.get("GUPSHUP_TEMPLATE_API_KEY", "")
            gupshup_source = _gupshup_template_config.get("GUPSHUP_TEMPLATE_SOURCE", "")
            gupshup_url = _gupshup_template_config.get(
                "GUPSHUP_TEMPLATE_URL",
                "https://api.gupshup.io/wa/api/v1/template/msg",
            )
            app_name = _gupshup_template_config.get("APP_NAME", "")
        else:
            gupshup_api_key = GUPSHUP_API_KEY
            gupshup_source = GUPSHUP_SOURCE
            gupshup_url = GUPSHUP_URL
            app_name = APP_NAME

        template_payload = {"id": template_id, "params": params or []}
        template_str = json.dumps(template_payload, separators=(",", ":"))
        image_url_norm = unquote(image_url) if image_url else None
        message_str = (
            json.dumps({"image": {"link": image_url_norm}, "type": "image"}, separators=(",", ":"))
            if image_url_norm
            else None
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
            client = await get_shared_async_http_client()
            response = await client.post(
                gupshup_url,
                data=urlencode(payload),
                headers=headers,
                timeout=10,
            )
            logger.info(f"[API_RESPONSE] Status: {response.status_code}, Body: {response.text}")
            if response.status_code in [200, 202]:
                success = True
                response_data = response.json()
            else:
                error_message = f"HTTP {response.status_code}: {response.text}"
        except Exception as e:
            logger.error(f"[API_CALL] Error sending Shopify template: {e}")
            error_message = str(e)

    if client_id:
        try:
            from fashion_bot.utils.template_delivery_logger import alog_template_delivery

            message_id = None
            if isinstance(response_data, dict):
                message_id = (
                    response_data.get("messageId")
                    or response_data.get("id")
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
            from fashion_bot.utils.conversation_message_adder import aadd_template_to_conversation_safe

            conv_id = await aadd_template_to_conversation_safe(
                client_id=client_id,
                phone_number=phone,
                template_message=template_message,
                channel_type="whatsapp",
                template_id=template_id,
                template_name=template_name,
            )
            if conv_id:
                logger.info(f"[SHOPIFY] ✅ Template-initiated conversation created: {conv_id}")
            else:
                logger.warning("[SHOPIFY] ⚠️  Could not add template message to conversation")
        except Exception as conv_error:
            logger.warning(f"[SHOPIFY] ⚠️  Error adding template to conversation: {conv_error}")

    return response_data if success else None
