"""
Widget configuration and per-client widget settings endpoints.
"""

import asyncio
import hashlib
import logging
import json
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse

from fashion_bot.config_manager import (
    aget_config,
    aget_json_config,
    begin_config_read_tracking,
    config_read_state,
    is_config_flag_enabled,
)
from fashion_bot.database_manager import awith_retry, get_async_postgres_connection
from fashion_bot.env_loader import get_bool, get_env, get_int
from fashion_bot.utils.client_id_utils import decode_client_id, is_encoded_client_id
from fashion_bot.utils.redis_client import get_shared_async_redis_client
from fashion_bot.utils.tiered_cache import aget_with_tiered_cache, ainvalidate_tiered_cache_key

WIDGET_CLIENT_NAME_TTL = get_int("WIDGET_CLIENT_NAME_TTL", 600)

logger = logging.getLogger(__name__)

# Widget version from environment (allows instant rollouts)

# Optional: split CDN (static) vs API (WebSocket, non-static JSON). Embedders load loader from CDN; config still hits API.
WIDGET_API_ORIGIN = get_env("WIDGET_API_ORIGIN", "").strip().rstrip("/")
WIDGET_CDN_STATIC_ORIGIN = get_env("WIDGET_CDN_STATIC_ORIGIN", "").strip().rstrip("/")
WIDGET_VERSION = get_env("WIDGET_VERSION", "v78")
ENABLE_WIDGET_SUGGESTIONS_CONFIG = get_bool("ENABLE_WIDGET_SUGGESTIONS_CONFIG", False)
ENABLE_WIDGET_BUTTON_GROUPS_CONFIG = get_bool("ENABLE_WIDGET_BUTTON_GROUPS_CONFIG", True)
ENABLE_WIDGET_LAUNCHER_HINTS_CONFIG = get_bool("ENABLE_WIDGET_LAUNCHER_HINTS_CONFIG", False)
ENABLE_WIDGET_LAUNCHER_THEME_CONFIG = get_bool("ENABLE_WIDGET_LAUNCHER_THEME_CONFIG", False)
ENABLE_WIDGET_FARO = get_bool("ENABLE_WIDGET_FARO", False)
WIDGET_FARO_COLLECTOR_URL = get_env("WIDGET_FARO_COLLECTOR_URL", "")
WIDGET_FARO_APP_NAME = get_env("WIDGET_FARO_APP_NAME", "fashion-bot-chat-widget")
WIDGET_FARO_ENVIRONMENT = get_env("WIDGET_FARO_ENVIRONMENT", "production")
ENABLE_WIDGET_FARO_TRACING = get_bool("ENABLE_WIDGET_FARO_TRACING", True)
# Comma-separated hostnames for Faro beforeSend filtering (widget JS only; storefront noise dropped).
WIDGET_FARO_ALLOWED_DOMAINS = get_env("WIDGET_FARO_ALLOWED_DOMAINS", "").strip()
ENABLE_WIDGET_CLARITY = get_bool("ENABLE_WIDGET_CLARITY", False)
WIDGET_CLARITY_PROJECT_ID = get_env("WIDGET_CLARITY_PROJECT_ID", "")
WIDGET_MIN_SUPPORTED_VERSION = int(get_env("WIDGET_MIN_SUPPORTED_VERSION", "36"))
WIDGET_PHONE_NUDGE_MIN_VERSION = int(get_env("WIDGET_PHONE_NUDGE_MIN_VERSION", str(WIDGET_MIN_SUPPORTED_VERSION)))
WIDGET_THEME_MIN_VERSION = int(get_env("WIDGET_THEME_MIN_VERSION", str(WIDGET_MIN_SUPPORTED_VERSION)))
WIDGET_HEADER_AVATAR_MIN_VERSION = int(get_env("WIDGET_HEADER_AVATAR_MIN_VERSION", str(WIDGET_MIN_SUPPORTED_VERSION)))
WIDGET_VARIANT_UI_MIN_VERSION = int(get_env("WIDGET_VARIANT_UI_MIN_VERSION", str(WIDGET_MIN_SUPPORTED_VERSION)))
WIDGET_FARO_MIN_VERSION = int(get_env("WIDGET_FARO_MIN_VERSION", "38"))
WIDGET_CLARITY_MIN_VERSION = int(get_env("WIDGET_CLARITY_MIN_VERSION", "39"))
# /widget/config.json is fetched on every storefront page view. See the
# response headers at the end of get_widget_config for why this exists.
WIDGET_CONFIG_CACHE_SECONDS = int(get_env("WIDGET_CONFIG_CACHE_SECONDS", "60"))
WIDGET_LOGO_CACHE_SECONDS = int(get_env("WIDGET_LOGO_CACHE_SECONDS", "3600"))
WIDGET_LAUNCHER_THEME_CACHE_SECONDS = int(get_env("WIDGET_LAUNCHER_THEME_CACHE_SECONDS", "1800"))
MOBILE_WIDGET_TEXT_CACHE_SECONDS = int(get_env("MOBILE_WIDGET_TEXT_CACHE_SECONDS", "0"))
DESKTOP_WIDGET_TEXT_CACHE_SECONDS = int(get_env("DESKTOP_WIDGET_TEXT_CACHE_SECONDS", "0"))
WIDGET_HEADER_TEXT_CACHE_SECONDS = int(get_env("WIDGET_HEADER_TEXT_CACHE_SECONDS", "0"))
WIDGET_BUTTON_GROUPS_CACHE_SECONDS = int(get_env("WIDGET_BUTTON_GROUPS_CACHE_SECONDS", "0"))
WIDGET_QUICK_ACTIONS_CACHE_SECONDS = int(get_env("WIDGET_QUICK_ACTIONS_CACHE_SECONDS", "0"))
WIDGET_WINDOW_THEME_CACHE_SECONDS = int(get_env("WIDGET_WINDOW_THEME_CACHE_SECONDS", "0"))
WIDGET_STATUS_TEXT_CACHE_SECONDS = int(get_env("WIDGET_STATUS_TEXT_CACHE_SECONDS", "0"))
WIDGET_MESSAGE_THEME_CACHE_SECONDS = int(get_env("WIDGET_MESSAGE_THEME_CACHE_SECONDS", "0"))
WIDGET_SEND_BUTTON_THEME_CACHE_SECONDS = int(get_env("WIDGET_SEND_BUTTON_THEME_CACHE_SECONDS", "0"))
WIDGET_CHAT_INPUT_THEME_CACHE_SECONDS = int(get_env("WIDGET_CHAT_INPUT_THEME_CACHE_SECONDS", "0"))
WIDGET_PRODUCT_CARD_ACTION_THEME_CACHE_SECONDS = int(get_env("WIDGET_PRODUCT_CARD_ACTION_THEME_CACHE_SECONDS", "0"))
WIDGET_GEOLOCATION_CACHE_SECONDS = int(get_env("WIDGET_GEOLOCATION_CACHE_SECONDS", "0"))
WIDGET_POSITION_CONFIG_CACHE_SECONDS = int(get_env("WIDGET_POSITION_CONFIG_CACHE_SECONDS", "0"))
# The bundle needs its own TTL rather than the minimum of its sections': twelve
# of the sixteen default to 0, so a minimum would make the bundle no-store and
# there would be nothing to cache. 60s trades at most a minute of staleness on a
# branding change for collapsing 16 uncached round trips per page load into one.
# Set to 0 to opt out and keep the bundle uncached.
WIDGET_BUNDLE_CONFIG_CACHE_SECONDS = int(get_env("WIDGET_BUNDLE_CONFIG_CACHE_SECONDS", "60"))

# Create router
widget_router = APIRouter(tags=["widget"])


def _cache_headers(ttl_seconds: int) -> Dict[str, str]:
    if ttl_seconds <= 0:
        return {"Cache-Control": "no-store"}
    return {"Cache-Control": f"public, max-age={ttl_seconds}"}


def _parse_faro_allowed_domains(raw: str) -> List[str]:
    """Comma-separated hostnames for observability.faro.allowedDomains in widget config."""
    if not raw:
        return []
    out: List[str] = []
    for part in raw.split(","):
        host = part.strip().lower()
        if not host:
            continue
        # Allow accidental https://host/path paste
        if "://" in host:
            host = (urlparse(host).hostname or "").lower()
            if not host:
                continue
        if host:
            out.append(host.rstrip("/"))
    return out




def _default_stable_fallback_version(version: str) -> str:
    match = version[1:] if isinstance(version, str) and version.startswith("v") else ""
    try:
        version_number = int(match)
    except (TypeError, ValueError):
        return "v36"
    if version_number <= WIDGET_MIN_SUPPORTED_VERSION:
        return f"v{version_number}"
    return f"v{version_number - 1}"


WIDGET_STABLE_FALLBACK_VERSION = get_env(
    "WIDGET_STABLE_FALLBACK_VERSION",
    _default_stable_fallback_version(WIDGET_VERSION),
)


def _normalize_suggestion_item(item: Any) -> Optional[Dict[str, str]]:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        return {"text": text, "message": text}
    if not isinstance(item, dict):
        return None
    text = str(item.get("text") or item.get("label") or "").strip()
    message = str(item.get("message") or item.get("query") or text).strip()
    if not text or not message:
        return None
    return {"text": text, "message": message}


def _normalize_suggestion_list(items: Any) -> List[Dict[str, str]]:
    if not isinstance(items, list):
        return []
    normalized: List[Dict[str, str]] = []
    for item in items:
        suggestion = _normalize_suggestion_item(item)
        if suggestion:
            normalized.append(suggestion)
    return normalized


def _normalize_nested_suggestions(raw: Any) -> Dict[str, List[Dict[str, str]]]:
    if not isinstance(raw, dict):
        return {}
    normalized: Dict[str, List[Dict[str, str]]] = {}
    for key, value in raw.items():
        normalized_list = _normalize_suggestion_list(value)
        if normalized_list:
            normalized[str(key)] = normalized_list
    return normalized


def _normalize_chat_suggestions_config(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    post_query = raw.get("postQuery")
    if post_query is None:
        post_query = raw.get("post_query")

    normalized = {
        "home": _normalize_suggestion_list(raw.get("home")),
        "collection": _normalize_suggestion_list(raw.get("collection")),
        "cart": _normalize_suggestion_list(raw.get("cart")),
        "product": _normalize_nested_suggestions(raw.get("product")),
        "postQuery": _normalize_nested_suggestions(post_query),
    }

    return normalized


def _coerce_widget_geolocation_enabled(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        return default
    return bool(value)


def _normalize_widget_geolocation_config(raw: Any) -> Dict[str, bool]:
    """Per-client browser geolocation prompt config. Defaults to disabled when unset."""
    if raw is True:
        return {"enabled": True}
    if raw is False:
        return {"enabled": False}
    if isinstance(raw, str):
        return {"enabled": _coerce_widget_geolocation_enabled(raw, default=False)}

    if not isinstance(raw, dict):
        return {"enabled": False}

    payload = raw
    for nested_key in ("geolocation_config", "widget_geolocation_config", "geolocationConfig"):
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            payload = nested
            break

    enabled = payload.get("enabled")
    if enabled is None:
        enabled = payload.get("is_enabled")
    if enabled is None:
        enabled = payload.get("isEnabled")
    if enabled is None:
        enabled = payload.get("request_geolocation")
    if enabled is None:
        enabled = payload.get("requestGeolocation")
    if enabled is None:
        return {"enabled": False}

    return {"enabled": _coerce_widget_geolocation_enabled(enabled, default=False)}


def _resolve_geolocation_config_payload(raw: Any) -> Any:
    """Accept bool / dict / JSON string shapes stored in client_configs."""
    if raw is None or isinstance(raw, (dict, bool)):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        lowered = text.lower()
        if lowered in {"true", "false", "0", "1", "yes", "no", "on", "off"}:
            return text
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, bool)):
                return parsed
        except (json.JSONDecodeError, TypeError):
            return text
    return raw


def _normalize_desktop_widget_text_config(raw: Any) -> Dict[str, Any]:
    """Desktop-only launcher-text visibility toggle — a standalone config key
    (``desktop_widget_text``), deliberately not shared with ``mobile_widget_text``.

    Key absent (no client row, or ``isDisplay`` missing) → defaults to True
    (current behavior: desktop shows the launcher). Key present → its boolean
    value decides: False hides the launcher text (icon-only), True shows it.
    """
    if not isinstance(raw, dict):
        return {"isDisplay": True}
    value = raw.get("isDisplay")
    if value is None:
        value = raw.get("is_display")
    if value is None:
        return {"isDisplay": True}
    if isinstance(value, str):
        is_display = value.strip().lower() not in {"0", "false", "no", "n", "off"}
    else:
        is_display = bool(value)
    return {"isDisplay": is_display}


def _normalize_mobile_widget_text_config(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {"isDisplay": False, "textToDisplay": ""}

    is_display = raw.get("isDisplay")
    if is_display is None:
        is_display = raw.get("is_display")
    if isinstance(is_display, str):
        is_display_bool = is_display.strip().lower() in {"1", "true", "yes", "y", "on"}
    else:
        is_display_bool = bool(is_display)

    text = raw.get("textToDisplay")
    if text is None:
        text = raw.get("text_to_display")
    text_to_display = str(text or "").strip()

    if not is_display_bool or not text_to_display:
        return {"isDisplay": False, "textToDisplay": ""}

    normalized = {"isDisplay": True, "textToDisplay": text_to_display[:80]}

    mobile_text_color = _first_non_empty_text(raw, ("mobile_text_color", "mobileTextColor", "text_color", "textColor"))
    if mobile_text_color and any(
        _is_truthy_config_flag(raw.get(flag))
        for flag in ("apply_mobile_text_color", "applyMobileTextColor", "apply_text_color", "applyTextColor")
    ):
        normalized["mobile_text_color"] = mobile_text_color
        normalized["apply_mobile_text_color"] = True

    mobile_border_color = _first_non_empty_text(raw, ("mobile_border_color", "mobileBorderColor", "border_color", "borderColor"))
    if mobile_border_color and any(
        _is_truthy_config_flag(raw.get(flag))
        for flag in ("apply_mobile_border_color", "applyMobileBorderColor", "apply_border_color", "applyBorderColor")
    ):
        normalized["mobile_border_color"] = mobile_border_color
        normalized["apply_mobile_border_color"] = True

    header_text_color = _first_non_empty_text(raw, ("header_text_color", "headerTextColor", "text_color", "textColor"))
    if header_text_color and any(
        _is_truthy_config_flag(raw.get(flag))
        for flag in ("apply_header_text_color", "applyHeaderTextColor", "apply_text_color", "applyTextColor")
    ):
        normalized["header_text_color"] = header_text_color
        normalized["apply_header_text_color"] = True

    return normalized


def _normalize_widget_header_text_config(raw: Any) -> Dict[str, Any]:
    return _normalize_mobile_widget_text_config(raw)


def _first_non_empty_text(raw: Dict[str, Any], aliases: tuple[str, ...], max_len: int = 240) -> str:
    for alias in aliases:
        value = raw.get(alias)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text[:max_len]
    return ""


def _is_truthy_config_flag(value: Any) -> bool:
    """Every widget flag here is opt-in, so an unrecognised value stays off.

    Delegates to the shared parse in config_manager rather than keeping a
    third copy of it. One behaviour change comes with that: a numeric ``1``
    now reads as on (it used to be rejected by the old ``value is True``
    check), which is what a hand-edited ``"flag": 1`` obviously means.
    """
    return is_config_flag_enabled(value, default=False)


def _normalize_widget_window_theme_config(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    theme = raw.get("window_theme") if isinstance(raw.get("window_theme"), dict) else raw
    if isinstance(raw.get("widget_window_theme"), dict):
        theme = raw.get("widget_window_theme")
    if not isinstance(theme, dict):
        return {}

    field_map = {
        "font_family": {
            "aliases": ("font_family", "fontFamily"),
            "flags": ("apply_font_family", "applyFontFamily"),
        },
        "custom_font_family": {
            "aliases": ("custom_font_family", "customFontFamily"),
            "flags": ("apply_custom_font_family", "applyCustomFontFamily"),
        },
        "background_color": {
            "aliases": ("background_color", "backgroundColor", "background", "bg_color", "bgColor"),
            "flags": ("apply_background_color", "applyBackgroundColor"),
        },
        "header_background_color": {
            "aliases": (
            "header_background_color",
            "headerBackgroundColor",
            "header_bg_color",
            "headerBgColor",
            ),
            "flags": ("apply_header_background_color", "applyHeaderBackgroundColor"),
        },
    }

    normalized: Dict[str, Any] = {}
    for output_key, settings in field_map.items():
        should_apply = any(_is_truthy_config_flag(theme.get(flag)) for flag in settings["flags"])
        if not should_apply:
            continue
        value = _first_non_empty_text(theme, settings["aliases"])
        if value:
            normalized[output_key] = value
            normalized[settings["flags"][0]] = True
    return normalized


def _normalize_flagged_theme_config(raw: Any, nested_key: str, field_map: Dict[str, Dict[str, tuple[str, ...]]]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    wrapper_key = f"widget_{nested_key}"
    if isinstance(raw.get(wrapper_key), dict):
        raw = raw.get(wrapper_key)
    theme = raw.get(nested_key) if isinstance(raw.get(nested_key), dict) else raw
    if not isinstance(theme, dict):
        return {}

    normalized: Dict[str, Any] = {}
    for output_key, settings in field_map.items():
        should_apply = any(_is_truthy_config_flag(theme.get(flag)) for flag in settings["flags"])
        if not should_apply:
            continue
        value = _first_non_empty_text(theme, settings["aliases"])
        if value:
            normalized[output_key] = value
            normalized[settings["flags"][0]] = True
    return normalized


def _normalize_widget_message_theme_config(raw: Any) -> Dict[str, Any]:
    return _normalize_flagged_theme_config(
        raw,
        "message_theme",
        {
            "bot_message_background_color": {
                "aliases": ("bot_message_background_color", "botMessageBackgroundColor"),
                "flags": ("apply_bot_message_background_color", "applyBotMessageBackgroundColor"),
            },
            "user_message_background_color": {
                "aliases": ("user_message_background_color", "userMessageBackgroundColor"),
                "flags": ("apply_user_message_background_color", "applyUserMessageBackgroundColor"),
            },
            "bot_message_text_color": {
                "aliases": ("bot_message_text_color", "botMessageTextColor"),
                "flags": ("apply_bot_message_text_color", "applyBotMessageTextColor"),
            },
            "user_message_text_color": {
                "aliases": ("user_message_text_color", "userMessageTextColor"),
                "flags": ("apply_user_message_text_color", "applyUserMessageTextColor"),
            },
            "bot_message_border_color": {
                "aliases": ("bot_message_border_color", "botMessageBorderColor"),
                "flags": ("apply_bot_message_border_color", "applyBotMessageBorderColor"),
            },
            "user_message_border_color": {
                "aliases": ("user_message_border_color", "userMessageBorderColor"),
                "flags": ("apply_user_message_border_color", "applyUserMessageBorderColor"),
            },
        },
    )


def _normalize_widget_send_button_theme_config(raw: Any) -> Dict[str, Any]:
    return _normalize_flagged_theme_config(
        raw,
        "send_button_theme",
        {
            "send_button_background_color": {
                "aliases": ("send_button_background_color", "sendButtonBackgroundColor"),
                "flags": ("apply_send_button_background_color", "applySendButtonBackgroundColor"),
            },
        },
    )


def _normalize_widget_chat_input_theme_config(raw: Any) -> Dict[str, Any]:
    return _normalize_flagged_theme_config(
        raw,
        "chat_input_theme",
        {
            "chat_input_border_color": {
                "aliases": ("chat_input_border_color", "chatInputBorderColor"),
                "flags": ("apply_chat_input_border_color", "applyChatInputBorderColor"),
            },
            "chat_input_focus_border_color": {
                "aliases": ("chat_input_focus_border_color", "chatInputFocusBorderColor"),
                "flags": ("apply_chat_input_focus_border_color", "applyChatInputFocusBorderColor"),
            },
        },
    )


def _normalize_widget_product_card_action_theme_config(raw: Any) -> Dict[str, Any]:
    return _normalize_flagged_theme_config(
        raw,
        "product_card_action_theme",
        {
            "view_button_text_color": {
                "aliases": ("view_button_text_color", "viewButtonTextColor"),
                "flags": ("apply_view_button_text_color", "applyViewButtonTextColor"),
            },
            "view_button_border_color": {
                "aliases": ("view_button_border_color", "viewButtonBorderColor"),
                "flags": ("apply_view_button_border_color", "applyViewButtonBorderColor"),
            },
            "view_button_background_color": {
                "aliases": ("view_button_background_color", "viewButtonBackgroundColor"),
                "flags": ("apply_view_button_background_color", "applyViewButtonBackgroundColor"),
            },
            "add_to_cart_button_text_color": {
                "aliases": ("add_to_cart_button_text_color", "addToCartButtonTextColor"),
                "flags": ("apply_add_to_cart_button_text_color", "applyAddToCartButtonTextColor"),
            },
            "add_to_cart_button_border_color": {
                "aliases": ("add_to_cart_button_border_color", "addToCartButtonBorderColor"),
                "flags": ("apply_add_to_cart_button_border_color", "applyAddToCartButtonBorderColor"),
            },
            "add_to_cart_button_background_color": {
                "aliases": ("add_to_cart_button_background_color", "addToCartButtonBackgroundColor"),
                "flags": ("apply_add_to_cart_button_background_color", "applyAddToCartButtonBackgroundColor"),
            },
        },
    )


def _normalize_widget_status_text_config(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        text = raw.strip()
        return {
            "isDisplay": bool(text),
            "textToDisplay": text[:120],
            "poweredByText": "Powered by Bloomerce" if text else "",
        }

    if not isinstance(raw, dict):
        return {"isDisplay": False, "textToDisplay": "", "poweredByText": ""}

    status = raw.get("status_text") if isinstance(raw.get("status_text"), dict) else raw
    if isinstance(raw.get("widget_status_text"), dict):
        status = raw.get("widget_status_text")
    if not isinstance(status, dict):
        return {"isDisplay": False, "textToDisplay": "", "poweredByText": ""}

    is_display = status.get("isDisplay")
    if is_display is None:
        is_display = status.get("is_display", True)
    if isinstance(is_display, str):
        is_display_bool = is_display.strip().lower() in {"1", "true", "yes", "y", "on"}
    else:
        is_display_bool = bool(is_display)

    text_to_display = _first_non_empty_text(status, ("textToDisplay", "text_to_display", "text", "label"), max_len=120)
    powered_by_text = _first_non_empty_text(
        status,
        ("poweredByText", "powered_by_text", "poweredBy", "powered_by"),
        max_len=80,
    )
    status_text_background_color = _first_non_empty_text(
        status,
        ("status_text_background_color", "statusTextBackgroundColor", "background_color", "backgroundColor"),
        max_len=120,
    )
    apply_status_text_background_color = any(
        _is_truthy_config_flag(status.get(flag))
        for flag in ("apply_status_text_background_color", "applyStatusTextBackgroundColor")
    )
    status_text_color = _first_non_empty_text(
        status,
        ("status_text_color", "statusTextColor", "text_color", "textColor"),
        max_len=120,
    )
    apply_status_text_color = any(
        _is_truthy_config_flag(status.get(flag))
        for flag in ("apply_status_text_color", "applyStatusTextColor", "apply_text_color", "applyTextColor")
    )

    if not is_display_bool or not text_to_display:
        return {"isDisplay": False, "textToDisplay": "", "poweredByText": ""}

    normalized: Dict[str, Any] = {
        "isDisplay": True,
        "textToDisplay": text_to_display,
        "poweredByText": powered_by_text or "Powered by Bloomerce",
    }
    if status_text_background_color and apply_status_text_background_color:
        normalized["status_text_background_color"] = status_text_background_color
        normalized["apply_status_text_background_color"] = True
    if status_text_color and apply_status_text_color:
        normalized["status_text_color"] = status_text_color
        normalized["apply_status_text_color"] = True
    return normalized


def _normalize_launcher_hints_list(items: Any) -> List[str]:
    if not isinstance(items, list):
        return []
    normalized: List[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_widget_launcher_hints_config(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    normalized: Dict[str, Any] = {}

    default = _normalize_launcher_hints_list(raw.get("default"))
    if default:
        normalized["default"] = default

    by_client_id = raw.get("byClientId") or raw.get("by_client_id")
    if isinstance(by_client_id, dict):
        normalized_ids: Dict[str, List[str]] = {}
        for key, value in by_client_id.items():
            hints = _normalize_launcher_hints_list(value)
            if hints:
                normalized_ids[str(key)] = hints
        if normalized_ids:
            normalized["byClientId"] = normalized_ids

    by_client_name = raw.get("byClientName") or raw.get("by_client_name")
    if isinstance(by_client_name, dict):
        normalized_names: Dict[str, List[str]] = {}
        for key, value in by_client_name.items():
            hints = _normalize_launcher_hints_list(value)
            if hints:
                normalized_names[str(key)] = hints
        if normalized_names:
            normalized["byClientName"] = normalized_names

    by_domain = raw.get("byDomainSubstring") or raw.get("by_domain_substring")
    if isinstance(by_domain, dict):
        normalized_domains: Dict[str, List[str]] = {}
        for key, value in by_domain.items():
            hints = _normalize_launcher_hints_list(value)
            if hints:
                normalized_domains[str(key)] = hints
        if normalized_domains:
            normalized["byDomainSubstring"] = normalized_domains

    return normalized


def _normalize_widget_launcher_theme_config(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}

    theme = raw.get("launcher_theme") if isinstance(raw.get("launcher_theme"), dict) else raw
    if not isinstance(theme, dict):
        return {}

    field_map = {
        "background": ("background",),
        "text_color": ("text_color", "textColor"),
        "caret_color": ("caret_color", "caretColor"),
        "border_color": ("border_color", "borderColor"),
        "ring_color": ("ring_color", "ringColor"),
        "shadow": ("shadow",),
        "avatar_background": ("avatar_background", "avatarBackground"),
        "dot_border_color": ("dot_border_color", "dotBorderColor"),
        # Deliberately separate from "shadow" (tuned for the ~300px pill) —
        # only applied to the desktop icon-only avatar (~48px), which needs
        # its own, much smaller blur/spread value.
        "icon_shadow": ("icon_shadow", "iconShadow"),
    }

    normalized: Dict[str, str] = {}
    for output_key, aliases in field_map.items():
        for alias in aliases:
            value = theme.get(alias)
            if value is not None:
                text = str(value).strip()
                if text:
                    normalized[output_key] = text
                    break
    return normalized


def _normalize_widget_position_config(raw: Any) -> Optional[str]:
    if not isinstance(raw, dict):
        return None

    position = str(raw.get("positioning") or raw.get("position") or "").strip().lower()
    if position in {"bottom-left", "left"}:
        return "bottom-left"
    if position in {"bottom-right", "right"}:
        return "bottom-right"
    return None


def _normalize_widget_logo_config(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    logo_url = raw.get("logo_url") or raw.get("logoUrl")
    if logo_url is None:
        return {}
    text = str(logo_url).strip()
    if not text:
        return {}
    return {"logo_url": text}


def _normalize_widget_button_style(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}

    field_map = {
        "text_color": ("text_color", "textColor", "color"),
        "border_color": ("border_color", "borderColor", "border"),
        "background_color": ("background_color", "backgroundColor", "background", "bg_color", "bgColor"),
    }
    normalized: Dict[str, str] = {}
    for output_key, aliases in field_map.items():
        for alias in aliases:
            value = raw.get(alias)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                normalized[output_key] = text[:120]
                break
    return normalized


def _normalize_widget_button_image(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {"type": "none", "url": "", "alt_text": ""}

    image_type = str(raw.get("type") or "none").strip().lower()
    image_url = str(raw.get("url") or raw.get("src") or "").strip()
    alt_text = str(raw.get("alt_text") or raw.get("altText") or "").strip()

    if not image_url:
        image_type = "none"

    return {
        "type": image_type[:40],
        "url": image_url[:1000],
        "alt_text": alt_text[:120],
    }


def _normalize_widget_button_item(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str):
        label = item.strip()
        if not label:
            return None
        return {
            "id": "",
            "label": label[:80],
            "action_value": label[:160],
            "style": {},
            "image": {"type": "none", "url": "", "alt_text": label[:80]},
            "inherit_group_style": True,
        }

    if not isinstance(item, dict):
        return None

    label = str(item.get("label") or item.get("text") or "").strip()
    action_value = str(
        item.get("action_value")
        or item.get("actionValue")
        or item.get("message")
        or item.get("query")
        or label
    ).strip()
    if not label or not action_value:
        return None

    inherit_group_style = item.get("inherit_group_style")
    if inherit_group_style is None:
        inherit_group_style = item.get("inheritGroupStyle", True)

    return {
        "id": str(item.get("id") or "").strip()[:80],
        "label": label[:80],
        "action_value": action_value[:200],
        "style": _normalize_widget_button_style(item.get("style")),
        "image": _normalize_widget_button_image(item.get("image")),
        "inherit_group_style": bool(inherit_group_style),
    }


def _normalize_widget_button_placement(value: Any) -> str:
    text = str(value or "top-buttons").strip().lower().replace("_", "-")
    if text in {"top-buttons", "top", "suggestions"}:
        return "top-buttons"
    if text in {"above-chat", "quick-actions", "bottom-buttons", "quick-actions-panel"}:
        return "above-chat"
    return "top-buttons"


def _normalize_widget_button_display_mode(value: Any) -> str:
    text = str(value or "show-at-start").strip().lower().replace("_", "-")
    if text in {"post-chat", "after-chat", "post-query", "after-reply"}:
        return "post-chat"
    if text in {"always", "all"}:
        return "always"
    if text in {"show-with-history", "with-history", "history", "restored-history"}:
        return "show-with-history"
    return "show-at-start"


def _normalize_widget_quick_action_item(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    action_id = str(item.get("id") or item.get("action_id") or item.get("actionId") or "").strip()
    text_to_display = str(item.get("textToDisplay") or item.get("text_to_display") or item.get("label") or "").strip()
    action_value = str(item.get("action_value") or item.get("actionValue") or item.get("message") or "").strip()
    emoji = str(item.get("emoji") or "").strip()
    is_display = item.get("isDisplay")
    if is_display is None:
        is_display = item.get("is_display", True)

    if not action_id:
        return None

    return {
        "id": action_id[:80],
        "emoji": emoji[:20],
        "image": _normalize_widget_button_image(item.get("image")),
        "style": _normalize_widget_button_style(item.get("style")),
        "isDisplay": bool(is_display),
        "action_value": action_value[:200],
        "textToDisplay": text_to_display[:80],
    }


def _normalize_widget_quick_actions_config(raw: Any) -> Dict[str, Any]:
    actions = raw.get("actions") if isinstance(raw, dict) else raw
    if not isinstance(actions, list):
        return {"actions": []}

    normalized_actions: List[Dict[str, Any]] = []
    for action in actions[:12]:
        normalized = _normalize_widget_quick_action_item(action)
        if normalized:
            normalized_actions.append(normalized)

    return {"actions": normalized_actions}


def _normalize_widget_button_groups_config(raw: Any) -> Dict[str, Any]:
    groups = raw.get("groups") if isinstance(raw, dict) else raw
    if not isinstance(groups, list):
        return {"groups": []}

    normalized_groups: List[Dict[str, Any]] = []
    for idx, group in enumerate(groups[:10]):
        if not isinstance(group, dict):
            continue
        buttons_raw = group.get("buttons")
        if not isinstance(buttons_raw, list):
            continue

        buttons = []
        for button in buttons_raw[:12]:
            normalized_button = _normalize_widget_button_item(button)
            if normalized_button:
                buttons.append(normalized_button)
        if not buttons:
            continue

        group_style = _normalize_widget_button_style(group.get("style"))
        for button in buttons:
            if button.get("inherit_group_style"):
                button["style"] = {**button.get("style", {}), **group_style}

        suggestion_mode = str(group.get("suggestion_mode") or group.get("suggestionMode") or "override-config").strip().lower().replace("_", "-")
        if suggestion_mode not in {"override-config", "append-config"}:
            suggestion_mode = "override-config"

        normalized_groups.append({
            "id": str(group.get("id") or f"group-{idx + 1}").strip()[:80],
            "name": str(group.get("name") or "").strip()[:120],
            "placement": _normalize_widget_button_placement(group.get("placement")),
            "display_mode": _normalize_widget_button_display_mode(group.get("display_mode") or group.get("displayMode")),
            "suggestion_mode": suggestion_mode,
            "style": group_style,
            "buttons": buttons,
        })

    return {"groups": normalized_groups}


@awith_retry
async def _aread_client_id_for_name(normalized_name: str) -> Optional[str]:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                'SELECT id FROM clients WHERE LOWER("name") = %s LIMIT 1',
                (normalized_name,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return str(row.get("id") if isinstance(row, dict) else row[0])


async def _aresolve_client_id(client_name: Optional[str]) -> Optional[str]:
    """Resolve a widget client_name -> client_id via tiered cache.

    Memory -> Redis -> Postgres per AGENTS.md §3. Negatives cached in
    memory only to avoid DB pressure from unmapped names.
    """
    normalized_name = (client_name or "").strip().lower()
    if not normalized_name:
        return None

    cache_key = f"widget:client_id:{normalized_name}"

    async def _load_from_db() -> Optional[str]:
        try:
            return await _aread_client_id_for_name(normalized_name)
        except Exception as e:
            logger.error("widget _aresolve_client_id DB error for '%s': %s", normalized_name, e)
            return None

    async def _redis_get() -> Optional[str]:
        client = await get_shared_async_redis_client()
        if not client:
            return None
        raw = await client.get(cache_key)
        return raw if raw else None

    async def _redis_set(value: Optional[str]) -> None:
        if not value:
            return
        client = await get_shared_async_redis_client()
        if client:
            await client.setex(cache_key, WIDGET_CLIENT_NAME_TTL, value)

    value, _src = await aget_with_tiered_cache(
        cache_key=cache_key,
        ttl_seconds=WIDGET_CLIENT_NAME_TTL,
        load_from_source_fn=_load_from_db,
        get_from_redis_fn=_redis_get,
        set_to_redis_fn=_redis_set,
        cache_none=True,
    )
    return value


async def _aresolve_effective_client_id(
    client_id: Optional[str],
    client_name: Optional[str],
) -> Optional[str]:
    normalized_client_id = (client_id or "").strip()
    if normalized_client_id:
        if is_encoded_client_id(normalized_client_id):
            try:
                return decode_client_id(normalized_client_id)
            except ValueError:
                logger.warning("Widget config received invalid encoded client_id", extra={"client_id": normalized_client_id[:20]})
                return None
        return normalized_client_id
    return await _aresolve_client_id(client_name)


@widget_router.get("/widget/config.json")
async def get_widget_config(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """
    Returns current widget configuration.
    
    Response:
    {
        "version": "v1",
        "bundle": "chat-widget.v1.js",
        "features": { ... }
    }
    
    Cache: 1 minute (short TTL for fast rollouts)
    """
    config = {
        "version": WIDGET_VERSION,
        "bundle": f"chat-widget.{WIDGET_VERSION}.js",
        "fallbackVersion": WIDGET_STABLE_FALLBACK_VERSION,
        "versionCaps": {
            "minSupportedVersion": WIDGET_MIN_SUPPORTED_VERSION,
            "phoneNudgeMin": WIDGET_PHONE_NUDGE_MIN_VERSION,
            "themeMin": WIDGET_THEME_MIN_VERSION,
            "headerAvatarMin": WIDGET_HEADER_AVATAR_MIN_VERSION,
            "variantUiMin": WIDGET_VARIANT_UI_MIN_VERSION,
            "faroMin": WIDGET_FARO_MIN_VERSION,
            "clarityMin": WIDGET_CLARITY_MIN_VERSION,
        },
        "features": {
            "indexedDB": True,
            "lazyLoad": True,
            "contextDetection": True,
            "productCarousel": True,
            "clientSuggestionConfig": ENABLE_WIDGET_SUGGESTIONS_CONFIG,
            "clientButtonGroupsConfig": ENABLE_WIDGET_BUTTON_GROUPS_CONFIG,
            "clientQuickActionsConfig": True,
            "clientLauncherHintsConfig": ENABLE_WIDGET_LAUNCHER_HINTS_CONFIG,
            "clientLauncherThemeConfig": ENABLE_WIDGET_LAUNCHER_THEME_CONFIG,
            "clientGeolocationConfig": True,
            "clientWindowThemeConfig": True,
            "clientStatusTextConfig": True,
        }
    }
    if WIDGET_API_ORIGIN:
        config["apiBaseUrl"] = WIDGET_API_ORIGIN
    if WIDGET_CDN_STATIC_ORIGIN:
        config["staticBaseUrl"] = WIDGET_CDN_STATIC_ORIGIN

    position = "bottom-left"
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if effective_client_id:
        if WIDGET_POSITION_CONFIG_CACHE_SECONDS <= 0:
            await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_config_position")
        client_widget_config = await aget_json_config("widget_config_position", client_id=effective_client_id)
        position = _normalize_widget_position_config(client_widget_config) or position
    config["position"] = position
    config["clientConfig"] = {"position": position}
    # This response is no-store whenever a client was named, so it is safe to
    # carry the resolved id. It lets the widget address the bundle by id rather
    # than by a display name, which makes a much better cache key.
    config["resolvedClientId"] = effective_client_id
    # Lets a widget skip the bundle probe entirely on older backends.
    config["bundleConfig"] = True
    

    if ENABLE_WIDGET_FARO and WIDGET_FARO_COLLECTOR_URL:
        faro_cfg: Dict[str, Any] = {
            "enabled": True,
            "url": WIDGET_FARO_COLLECTOR_URL,
            "appName": WIDGET_FARO_APP_NAME,
            "environment": WIDGET_FARO_ENVIRONMENT,
            "tracingEnabled": ENABLE_WIDGET_FARO_TRACING,
        }
        allowed = _parse_faro_allowed_domains(WIDGET_FARO_ALLOWED_DOMAINS)
        if allowed:
            faro_cfg["allowedDomains"] = allowed
        config["observability"] = {"faro": faro_cfg}

    if ENABLE_WIDGET_CLARITY and WIDGET_CLARITY_PROJECT_ID:
        observability = config.setdefault("observability", {})
        observability["clarity"] = {
            "enabled": True,
            "projectId": WIDGET_CLARITY_PROJECT_ID,
        }

    logger.info(f"Widget config requested: version={WIDGET_VERSION}")

    # This response used to be `no-store` whenever client_id or client_name was
    # present -- i.e. on every real request, since the widget always sends both.
    #
    # That reads like tenant-isolation protection, and it is worth knowing it was
    # not. The same commit that added it also gave /widget/suggestions-config and
    # /widget/launcher-hints `public, max-age=1800`, and those take the identical
    # client_id/client_name params and return per-tenant data. The `no-store` was
    # about *freshness*: it landed together with the per-client `position` lookup
    # below (and its per-request tiered-cache invalidation), so a position change
    # -- and a WIDGET_VERSION rollback, which this endpoint also carries --
    # reached storefronts immediately.
    #
    # Caching per tenant is safe: client_id/client_name are query-string
    # parameters, and a CDN's default cache key is the full URL including the
    # query string, so entries are already scoped per client (AGENTS.md #7).
    # Verified against the live edge across five tenants -- 120 concurrent
    # interleaved requests, each returning its own client_id and its own launcher
    # theme, no cross-contamination.
    #
    # What it costs: a `position` edit now takes up to WIDGET_CONFIG_CACHE_SECONDS
    # to appear. A version rollback does not, because changing WIDGET_VERSION is
    # an env change, and the platform purges its edge cache on every deploy --
    # only the browser's own max-age window remains. Set the var to 0 to restore
    # `no-store` if a tenant genuinely needs instant config propagation.
    return JSONResponse(
        content=config,
        headers={
            **_cache_headers(WIDGET_CONFIG_CACHE_SECONDS),
            "X-Widget-Version": WIDGET_VERSION
        }
    )


@widget_router.get("/widget/suggestions-config")
async def get_widget_suggestions_config(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client chat suggestion config with a feature-flagged fallback."""
    if not ENABLE_WIDGET_SUGGESTIONS_CONFIG:
        return JSONResponse(
            content={
                "enabled": False,
                "source": "static",
                "client_id": client_id,
                "suggestions": {},
            },
            headers={"Cache-Control": "public, max-age=1800"},
        )

    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        logger.warning("Widget suggestions config requested without a resolvable client", extra={"client_name": client_name})
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "suggestions": {},
            },
            headers={"Cache-Control": "public, max-age=1800"},
        )

    config = await aget_json_config("chat_suggestions_config", client_id=effective_client_id)
    normalized = _normalize_chat_suggestions_config(config)
    source = "client_config" if normalized else "static"

    logger.info(
        "Widget suggestions config requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "suggestions": normalized,
        },
        headers={"Cache-Control": "public, max-age=1800"},
    )


@widget_router.get("/widget/button-groups")
async def get_widget_button_groups(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client widget button groups for chips and quick action cards."""
    if not ENABLE_WIDGET_BUTTON_GROUPS_CONFIG:
        return JSONResponse(
            content={
                "enabled": False,
                "source": "static",
                "client_id": client_id,
                "button_groups": {"groups": []},
            },
            headers=_cache_headers(WIDGET_BUTTON_GROUPS_CACHE_SECONDS),
        )

    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        logger.warning("Widget button groups requested without a resolvable client", extra={"client_name": client_name})
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "button_groups": {"groups": []},
            },
            headers=_cache_headers(WIDGET_BUTTON_GROUPS_CACHE_SECONDS),
        )

    if WIDGET_BUTTON_GROUPS_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_button_groups")
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_quick_buttons")

    config = await aget_json_config("widget_button_groups", client_id=effective_client_id)
    if not config:
        config = await aget_json_config("widget_quick_buttons", client_id=effective_client_id)
    normalized = _normalize_widget_button_groups_config(config)
    source = "client_config" if normalized.get("groups") else "static"

    logger.info(
        "Widget button groups requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "button_groups": normalized,
        },
        headers=_cache_headers(WIDGET_BUTTON_GROUPS_CACHE_SECONDS),
    )


@widget_router.get("/widget/quick-actions")
async def get_widget_quick_actions(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client bottom quick action visibility and labels."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        logger.warning("Widget quick actions requested without a resolvable client", extra={"client_name": client_name})
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "quick_actions": {"actions": []},
            },
            headers=_cache_headers(WIDGET_QUICK_ACTIONS_CACHE_SECONDS),
        )

    if WIDGET_QUICK_ACTIONS_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_quick_actions")

    config = await aget_json_config("widget_quick_actions", client_id=effective_client_id)
    normalized = _normalize_widget_quick_actions_config(config)
    source = "client_config" if normalized.get("actions") else "static"

    logger.info(
        "Widget quick actions requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "quick_actions": normalized,
        },
        headers=_cache_headers(WIDGET_QUICK_ACTIONS_CACHE_SECONDS),
    )


@widget_router.get("/widget/launcher-hints")
async def get_widget_launcher_hints(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client launcher hints with a feature-flagged fallback."""
    if not ENABLE_WIDGET_LAUNCHER_HINTS_CONFIG:
        return JSONResponse(
            content={
                "enabled": False,
                "source": "static",
                "client_id": client_id,
                "launcher_hints": {},
            },
            headers={"Cache-Control": "public, max-age=1800"},
        )

    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "launcher_hints": {},
            },
            headers={"Cache-Control": "public, max-age=1800"},
        )

    config = await aget_json_config("widget_launcher_hints", client_id=effective_client_id)
    normalized = _normalize_widget_launcher_hints_config(config)
    source = "client_config" if normalized else "static"

    logger.info(
        "Widget launcher hints requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "launcher_hints": normalized,
        },
        headers={"Cache-Control": "public, max-age=1800"},
    )


@widget_router.get("/widget/launcher-theme")
async def get_widget_launcher_theme(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client launcher theme with a feature-flagged fallback."""
    if not ENABLE_WIDGET_LAUNCHER_THEME_CONFIG:
        return JSONResponse(
            content={
                "enabled": False,
                "source": "static",
                "client_id": client_id,
                "launcher_theme": {},
            },
            headers=_cache_headers(WIDGET_LAUNCHER_THEME_CACHE_SECONDS),
        )

    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "launcher_theme": {},
            },
            headers=_cache_headers(WIDGET_LAUNCHER_THEME_CACHE_SECONDS),
        )

    if WIDGET_LAUNCHER_THEME_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_launcher_theme")

    config = await aget_json_config("widget_launcher_theme", client_id=effective_client_id)
    normalized = _normalize_widget_launcher_theme_config(config)
    source = "client_config" if normalized else "static"

    logger.info(
        "Widget launcher theme requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "launcher_theme": normalized,
        },
        headers=_cache_headers(WIDGET_LAUNCHER_THEME_CACHE_SECONDS),
    )


@widget_router.get("/widget/logo")
async def get_widget_logo(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client widget logo configuration."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "widget_logo": {},
            },
            headers={"Cache-Control": f"public, max-age={WIDGET_LOGO_CACHE_SECONDS}"},
        )

    config = await aget_json_config("widget_logo", client_id=effective_client_id)
    normalized = _normalize_widget_logo_config(config)
    source = "client_config" if normalized else "static"

    logger.info(
        "Widget logo requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "widget_logo": normalized,
        },
        headers={"Cache-Control": f"public, max-age={WIDGET_LOGO_CACHE_SECONDS}"},
    )


@widget_router.get("/widget/mobile-widget-text")
async def get_mobile_widget_text(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client mobile-only launcher text configuration."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "mobile_widget_text": {"isDisplay": False, "textToDisplay": ""},
            },
            headers=_cache_headers(MOBILE_WIDGET_TEXT_CACHE_SECONDS),
        )

    if MOBILE_WIDGET_TEXT_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:mobile_widget_text")

    config = await aget_json_config("mobile_widget_text", client_id=effective_client_id)
    normalized = _normalize_mobile_widget_text_config(config)
    source = "client_config" if isinstance(config, dict) else "static"

    logger.info(
        "Mobile widget text requested",
        extra={"client_id": effective_client_id, "source": source, "is_display": normalized["isDisplay"]},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "mobile_widget_text": normalized,
        },
        headers=_cache_headers(MOBILE_WIDGET_TEXT_CACHE_SECONDS),
    )


@widget_router.get("/widget/desktop-widget-text")
async def get_desktop_widget_text(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client desktop-only launcher display toggle.

    Standalone config key (``desktop_widget_text``), independent of
    ``mobile_widget_text``. Absent key or missing client → isDisplay=True
    (current desktop behavior: launcher shown).
    """
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "desktop_widget_text": {"isDisplay": True},
            },
            headers=_cache_headers(DESKTOP_WIDGET_TEXT_CACHE_SECONDS),
        )

    if DESKTOP_WIDGET_TEXT_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:desktop_widget_text")

    config = await aget_json_config("desktop_widget_text", client_id=effective_client_id)
    normalized = _normalize_desktop_widget_text_config(config)
    source = "client_config" if isinstance(config, dict) else "static"

    logger.info(
        "Desktop widget text requested",
        extra={"client_id": effective_client_id, "source": source, "is_display": normalized["isDisplay"]},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "desktop_widget_text": normalized,
        },
        headers=_cache_headers(DESKTOP_WIDGET_TEXT_CACHE_SECONDS),
    )


@widget_router.get("/widget/geolocation-config")
async def get_widget_geolocation_config(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client browser geolocation prompt configuration."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "geolocation_config": {"enabled": False},
            },
            headers=_cache_headers(WIDGET_GEOLOCATION_CACHE_SECONDS),
        )

    await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_geolocation_config")
    await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:geolocation_config")

    config = None
    config_source_key = "widget_geolocation_config"
    for key in ("widget_geolocation_config", "geolocation_config"):
        raw = await aget_config(key, client_id=effective_client_id)
        if raw is not None:
            config_source_key = key
            config = _resolve_geolocation_config_payload(raw)
            break
    normalized = _normalize_widget_geolocation_config(config)
    source = "client_config" if config is not None else "static"

    logger.info(
        "Widget geolocation config requested",
        extra={
            "client_id": effective_client_id,
            "source": source,
            "config_key": config_source_key,
            "request_geolocation": normalized["enabled"],
        },
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "geolocation_config": normalized,
        },
        headers=_cache_headers(WIDGET_GEOLOCATION_CACHE_SECONDS),
    )


@widget_router.get("/widget/header-text")
async def get_widget_header_text(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client open-chat header title configuration."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "widget_header_text": {"isDisplay": False, "textToDisplay": ""},
            },
            headers=_cache_headers(WIDGET_HEADER_TEXT_CACHE_SECONDS),
        )

    if WIDGET_HEADER_TEXT_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_header_text")

    config = await aget_json_config("widget_header_text", client_id=effective_client_id)
    normalized = _normalize_widget_header_text_config(config)
    source = "client_config" if isinstance(config, dict) else "static"

    logger.info(
        "Widget header text requested",
        extra={"client_id": effective_client_id, "source": source, "is_display": normalized["isDisplay"]},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "widget_header_text": normalized,
        },
        headers=_cache_headers(WIDGET_HEADER_TEXT_CACHE_SECONDS),
    )


@widget_router.get("/widget/window-theme")
async def get_widget_window_theme(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client open widget window theme configuration."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "widget_window_theme": {},
            },
            headers=_cache_headers(WIDGET_WINDOW_THEME_CACHE_SECONDS),
        )

    if WIDGET_WINDOW_THEME_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_window_theme")

    config = await aget_json_config("widget_window_theme", client_id=effective_client_id)
    normalized = _normalize_widget_window_theme_config(config)
    source = "client_config" if normalized else "static"

    logger.info(
        "Widget window theme requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "widget_window_theme": normalized,
        },
        headers=_cache_headers(WIDGET_WINDOW_THEME_CACHE_SECONDS),
    )


@widget_router.get("/widget/status-text")
async def get_widget_status_text(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client status strip text shown above the widget input."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "widget_status_text": {"isDisplay": False, "textToDisplay": "", "poweredByText": ""},
            },
            headers=_cache_headers(WIDGET_STATUS_TEXT_CACHE_SECONDS),
        )

    if WIDGET_STATUS_TEXT_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_status_text")

    config = await aget_json_config("widget_status_text", client_id=effective_client_id)
    normalized = _normalize_widget_status_text_config(config)
    source = "client_config" if config is not None else "static"

    logger.info(
        "Widget status text requested",
        extra={"client_id": effective_client_id, "source": source, "is_display": normalized["isDisplay"]},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "widget_status_text": normalized,
        },
        headers=_cache_headers(WIDGET_STATUS_TEXT_CACHE_SECONDS),
    )


@widget_router.get("/widget/message-theme")
async def get_widget_message_theme(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client user/bot message bubble theme configuration."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "widget_message_theme": {},
            },
            headers=_cache_headers(WIDGET_MESSAGE_THEME_CACHE_SECONDS),
        )

    if WIDGET_MESSAGE_THEME_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_message_theme")

    config = await aget_json_config("widget_message_theme", client_id=effective_client_id)
    normalized = _normalize_widget_message_theme_config(config)
    source = "client_config" if normalized else "static"

    logger.info(
        "Widget message theme requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "widget_message_theme": normalized,
        },
        headers=_cache_headers(WIDGET_MESSAGE_THEME_CACHE_SECONDS),
    )


@widget_router.get("/widget/send-button-theme")
async def get_widget_send_button_theme(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client send button theme configuration."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "widget_send_button_theme": {},
            },
            headers=_cache_headers(WIDGET_SEND_BUTTON_THEME_CACHE_SECONDS),
        )

    if WIDGET_SEND_BUTTON_THEME_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_send_button_theme")

    config = await aget_json_config("widget_send_button_theme", client_id=effective_client_id)
    normalized = _normalize_widget_send_button_theme_config(config)
    source = "client_config" if normalized else "static"

    logger.info(
        "Widget send button theme requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "widget_send_button_theme": normalized,
        },
        headers=_cache_headers(WIDGET_SEND_BUTTON_THEME_CACHE_SECONDS),
    )


@widget_router.get("/widget/chat-input-theme")
async def get_widget_chat_input_theme(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client chat input border theme configuration."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "widget_chat_input_theme": {},
            },
            headers=_cache_headers(WIDGET_CHAT_INPUT_THEME_CACHE_SECONDS),
        )

    if WIDGET_CHAT_INPUT_THEME_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_chat_input_theme")

    config = await aget_json_config("widget_chat_input_theme", client_id=effective_client_id)
    normalized = _normalize_widget_chat_input_theme_config(config)
    source = "client_config" if normalized else "static"

    logger.info(
        "Widget chat input theme requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "widget_chat_input_theme": normalized,
        },
        headers=_cache_headers(WIDGET_CHAT_INPUT_THEME_CACHE_SECONDS),
    )


@widget_router.get("/widget/product-card-action-theme")
async def get_widget_product_card_action_theme(
    client_id: Optional[str] = Query(default=None),
    client_name: Optional[str] = Query(default=None),
):
    """Return per-client product carousel View/Add to cart button theme configuration."""
    effective_client_id = await _aresolve_effective_client_id(client_id, client_name)
    if not effective_client_id:
        return JSONResponse(
            content={
                "enabled": True,
                "source": "missing_client",
                "client_id": None,
                "widget_product_card_action_theme": {},
            },
            headers=_cache_headers(WIDGET_PRODUCT_CARD_ACTION_THEME_CACHE_SECONDS),
        )

    if WIDGET_PRODUCT_CARD_ACTION_THEME_CACHE_SECONDS <= 0:
        await ainvalidate_tiered_cache_key(f"cfg:{effective_client_id}:widget_product_card_action_theme")

    config = await aget_json_config("widget_product_card_action_theme", client_id=effective_client_id)
    normalized = _normalize_widget_product_card_action_theme_config(config)
    source = "client_config" if normalized else "static"

    logger.info(
        "Widget product card action theme requested",
        extra={"client_id": effective_client_id, "source": source},
    )

    return JSONResponse(
        content={
            "enabled": True,
            "source": source,
            "client_id": effective_client_id,
            "widget_product_card_action_theme": normalized,
        },
        headers=_cache_headers(WIDGET_PRODUCT_CARD_ACTION_THEME_CACHE_SECONDS),
    )


@widget_router.get("/widget/health")
async def widget_health():
    """Health check for widget endpoints."""
    return {
        "status": "ok",
        "version": WIDGET_VERSION,
        "bundle": f"chat-widget.{WIDGET_VERSION}.js",
        "fallback_version": WIDGET_STABLE_FALLBACK_VERSION,
        "version_caps": {
            "min_supported_version": WIDGET_MIN_SUPPORTED_VERSION,
            "phone_nudge_min": WIDGET_PHONE_NUDGE_MIN_VERSION,
            "theme_min": WIDGET_THEME_MIN_VERSION,
            "header_avatar_min": WIDGET_HEADER_AVATAR_MIN_VERSION,
            "variant_ui_min": WIDGET_VARIANT_UI_MIN_VERSION,
            "faro_min": WIDGET_FARO_MIN_VERSION,
            "clarity_min": WIDGET_CLARITY_MIN_VERSION,
        },
        "client_suggestion_config": ENABLE_WIDGET_SUGGESTIONS_CONFIG,
        "client_button_groups_config": ENABLE_WIDGET_BUTTON_GROUPS_CONFIG,
        "client_launcher_hints_config": ENABLE_WIDGET_LAUNCHER_HINTS_CONFIG,
        "client_launcher_theme_config": ENABLE_WIDGET_LAUNCHER_THEME_CONFIG,
        "mobile_widget_text_cache_seconds": MOBILE_WIDGET_TEXT_CACHE_SECONDS,
        "widget_header_text_cache_seconds": WIDGET_HEADER_TEXT_CACHE_SECONDS,
        "widget_button_groups_cache_seconds": WIDGET_BUTTON_GROUPS_CACHE_SECONDS,
        "widget_quick_actions_cache_seconds": WIDGET_QUICK_ACTIONS_CACHE_SECONDS,
        "widget_window_theme_cache_seconds": WIDGET_WINDOW_THEME_CACHE_SECONDS,
        "widget_status_text_cache_seconds": WIDGET_STATUS_TEXT_CACHE_SECONDS,
        "widget_product_card_action_theme_cache_seconds": WIDGET_PRODUCT_CARD_ACTION_THEME_CACHE_SECONDS,
        "widget_geolocation_cache_seconds": WIDGET_GEOLOCATION_CACHE_SECONDS,
    }


# ---------------------------------------------------------------------------
# Bundled widget config
# ---------------------------------------------------------------------------
# One request instead of sixteen. Every section reuses its own endpoint handler
# verbatim, so a bundled section is byte-identical to what the standalone
# endpoint returns and the browser needs no new parsing. It also means future
# changes to a handler flow into the bundle automatically instead of drifting.
#
# The client reference lives in the path rather than a query string: caching
# layers vary in whether they key on, forward, or strip query parameters, and a
# path segment removes that class of problem entirely.

def _bundle_sections():
    """section name -> (handler, cache TTL seconds). Names match the URL suffix
    of the standalone endpoint so callers can fall back to it by name."""
    return {
        "launcher-theme": (get_widget_launcher_theme, WIDGET_LAUNCHER_THEME_CACHE_SECONDS),
        "launcher-hints": (get_widget_launcher_hints, 1800),
        "logo": (get_widget_logo, 1800),
        "mobile-widget-text": (get_mobile_widget_text, MOBILE_WIDGET_TEXT_CACHE_SECONDS),
        "desktop-widget-text": (get_desktop_widget_text, DESKTOP_WIDGET_TEXT_CACHE_SECONDS),
        "suggestions-config": (get_widget_suggestions_config, 1800),
        "button-groups": (get_widget_button_groups, WIDGET_BUTTON_GROUPS_CACHE_SECONDS),
        "quick-actions": (get_widget_quick_actions, WIDGET_QUICK_ACTIONS_CACHE_SECONDS),
        "geolocation-config": (get_widget_geolocation_config, WIDGET_GEOLOCATION_CACHE_SECONDS),
        "header-text": (get_widget_header_text, WIDGET_HEADER_TEXT_CACHE_SECONDS),
        "window-theme": (get_widget_window_theme, WIDGET_WINDOW_THEME_CACHE_SECONDS),
        "status-text": (get_widget_status_text, WIDGET_STATUS_TEXT_CACHE_SECONDS),
        "message-theme": (get_widget_message_theme, WIDGET_MESSAGE_THEME_CACHE_SECONDS),
        "send-button-theme": (get_widget_send_button_theme, WIDGET_SEND_BUTTON_THEME_CACHE_SECONDS),
        "chat-input-theme": (get_widget_chat_input_theme, WIDGET_CHAT_INPUT_THEME_CACHE_SECONDS),
        "product-card-action-theme": (
            get_widget_product_card_action_theme,
            WIDGET_PRODUCT_CARD_ACTION_THEME_CACHE_SECONDS,
        ),
    }


def _decode_section_response(response: Any) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(response.body)
    except Exception:
        return None


@widget_router.get("/widget/bundle-config/{client_ref}.json")
async def get_widget_bundle_config(
    client_ref: str = Path(..., description="client_id, encoded client id, or client name"),
):
    """Return every per-client widget config section in a single response."""
    reference = (client_ref or "").strip()
    # One path segment has to stand in for three things the query-string API kept
    # apart: an encoded id, a plain client_id, and a client name.
    # _aresolve_effective_client_id returns any non-encoded string unchanged, so
    # asking it first would accept "Acme Store" as a client_id and every section
    # would answer 200 with empty config - branding silently lost, and cached.
    # Resolve as a name first; a real client_id matches no clients.name row and
    # falls through unchanged.
    if reference and is_encoded_client_id(reference):
        effective_client_id = await _aresolve_effective_client_id(reference, None)
    else:
        effective_client_id = await _aresolve_client_id(reference) or (reference or None)

    sections_spec = _bundle_sections()
    names = list(sections_spec.keys())

    # Handlers resolve the client themselves, but passing an already-resolved
    # plain client_id makes each of those a no-op rather than 16 lookups.
    # aget_config swallows store errors and returns the default, so an outage
    # looks exactly like "nothing configured". Without this the bundle would be
    # judged complete and its empty sections cached for the full TTL.
    read_state, token = begin_config_read_tracking()
    try:
        results = await asyncio.gather(
            *(
                sections_spec[name][0](client_id=effective_client_id, client_name=None)
                for name in names
            ),
            return_exceptions=True,
        )
    finally:
        config_read_state.reset(token)
    store_degraded = read_state["failed"]

    sections: Dict[str, Any] = {}
    failed: List[str] = []
    for name, result in zip(names, results):
        payload = None if isinstance(result, BaseException) else _decode_section_response(result)
        if payload is None:
            # Omitted, never faked. A section carrying {"enabled": false} would
            # read to the browser as "feature switched off" and it would render
            # defaults; a missing section tells it to fetch that one endpoint.
            failed.append(name)
            continue
        sections[name] = payload

    # "Nothing is configured yet" and "we could not read the config" look the
    # same over HTTP, and caching either one pins an unbranded widget in front of
    # every shopper. A client that configures branding would otherwise wait out
    # the TTL before seeing it. Only cache a bundle that actually carries client
    # configuration.
    has_client_config = any(
        isinstance(payload, dict) and payload.get("source") == "client_config"
        for payload in sections.values()
    )

    if store_degraded:
        # Sections are still returned - stale-but-present beats nothing - but a
        # degraded read must never be pinned in front of every shopper.
        failed.append("config_store")

    if failed:
        logger.warning(
            "Widget bundle config had failing sections",
            extra={"client_id": effective_client_id, "failed_sections": failed},
        )

    body: Dict[str, Any] = {
        "bundle_version": 1,
        "client_id": effective_client_id,
        "sections": sections,
        "incomplete_sections": failed,
    }

    if not effective_client_id:
        headers = {"Cache-Control": "no-store"}
    else:
        # Never outlive a section that deliberately asks to be shorter-lived.
        # Sections at 0 are excluded: 0 means "no HTTP caching", and honouring
        # that here would make the bundle uncacheable for everyone.
        capped = [sections_spec[name][1] for name in sections if sections_spec[name][1] > 0]
        ttl = min([WIDGET_BUNDLE_CONFIG_CACHE_SECONDS] + capped)
        # Anything incomplete must not be cached, or one bad lookup is pinned
        # in front of every shopper for the whole TTL.
        headers = _cache_headers(0 if (failed or not has_client_config) else ttl)

    serialized = json.dumps(body, sort_keys=True, separators=(",", ":"))
    headers["ETag"] = '"' + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32] + '"'

    logger.info(
        "Widget bundle config requested",
        extra={
            "client_id": effective_client_id,
            "section_count": len(sections),
            "failed_count": len(failed),
            "has_client_config": has_client_config,
        },
    )
    return JSONResponse(content=body, headers=headers)
