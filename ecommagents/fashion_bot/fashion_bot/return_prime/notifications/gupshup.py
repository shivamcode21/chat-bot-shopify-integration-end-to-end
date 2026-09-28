"""Gupshup WhatsApp helpers for Return Prime webhook notifications."""

from __future__ import annotations

import json

import httpx

from fashion_bot.env_loader import get_env

GUPSHUP_DEFAULT_BASE_URL = "https://api.gupshup.io"


async def _aget_json_config_safe(config_key: str, client_id: str | None) -> dict:
    try:
        from fashion_bot.config_manager import aget_json_config

        return await aget_json_config(config_key, client_id=client_id) or {}
    except Exception:
        return {}


async def load_gupshup_config(client_id: str | None) -> dict:
    details = await _aget_json_config_safe("gupshup_details", client_id)
    default_country_code = (
        details.get("default_country_code")
        or details.get("country_calling_code")
        or details.get("calling_code")
        or get_env("GUPSHUP_DEFAULT_COUNTRY_CODE")
        or "91"
    )
    return {
        "api_key": details.get("api_key") or get_env("GUPSHUP_API_KEY"),
        "app_name": details.get("app_name") or get_env("GUPSHUP_APP_NAME"),
        "source_number": details.get("source_number") or get_env("GUPSHUP_SOURCE_NUMBER"),
        "default_country_code": "".join(ch for ch in str(default_country_code) if ch.isdigit()) or "91",
        "base_url": (
            details.get("base_url")
            or get_env("GUPSHUP_BASE_URL", GUPSHUP_DEFAULT_BASE_URL)
            or GUPSHUP_DEFAULT_BASE_URL
        ).rstrip("/"),
    }


async def load_template_config(client_id: str | None) -> dict:
    raw = await _aget_json_config_safe("gupshup_template_details", client_id)
    if isinstance(raw.get("templates"), dict):
        return raw["templates"]
    return raw


def _strip_phone_digits(phone: str | None) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


async def normalize_phone_for_whatsapp(phone: str | None, *, client_id: str | None) -> str | None:
    digits = _strip_phone_digits(phone)
    if not digits:
        return None
    if str(phone or "").strip().startswith("+"):
        return digits if 8 <= len(digits) <= 15 else None
    if 8 <= len(digits) <= 15 and len(digits) != 10:
        return digits
    if len(digits) == 10:
        config = await load_gupshup_config(client_id)
        default_country_code = "".join(
            ch for ch in str(config.get("default_country_code") or "") if ch.isdigit()
        ) or "91"
        if not default_country_code:
            return None
        return f"{default_country_code}{digits}"
    return None


async def send_template_message(
    *,
    client_id: str | None,
    destination: str,
    template_id: str,
    template_params: list[str],
) -> dict:
    config = await load_gupshup_config(client_id)
    if not config.get("api_key") or not config.get("app_name") or not config.get("source_number"):
        return {
            "success": False,
            "status_code": 500,
            "error": "Missing Gupshup API configuration.",
            "raw": None,
            "message_id": None,
        }

    body = {
        "channel": "whatsapp",
        "source": config["source_number"],
        "destination": destination,
        "src.name": config["app_name"],
        "template": json.dumps({"id": template_id, "params": template_params}),
    }
    headers = {
        "apikey": config["api_key"],
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            response = await http.post(
                f"{config['base_url']}/sm/api/v1/template/msg",
                data=body,
                headers=headers,
            )
    except Exception as exc:
        return {
            "success": False,
            "status_code": 0,
            "error": str(exc),
            "raw": None,
            "message_id": None,
        }

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw_text": response.text}

    message_id = None
    if isinstance(payload, dict):
        message_id = payload.get("messageId") or payload.get("message_id") or payload.get("id")

    error_message = None
    if isinstance(payload, dict):
        error_message = payload.get("message") or payload.get("error")

    return {
        "success": response.is_success,
        "status_code": response.status_code,
        "error": None
        if response.is_success
        else error_message or f"Gupshup API failed with status {response.status_code}.",
        "raw": payload,
        "message_id": message_id,
    }
