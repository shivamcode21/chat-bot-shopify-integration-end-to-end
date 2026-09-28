"""
Back-compat shim. The real Gupshup template sender now lives at
``fashion_bot/shipping/webhook/gupshup_template_sender.py`` and is
vendor-agnostic. This module re-exports the legacy name so any existing
imports keep working; new code should import from the shared location and
pass ``log_tag="SHIPROCKET"``.
"""
from __future__ import annotations

from typing import Any, List, Optional

from fashion_bot.shipping.webhook.gupshup_template_sender import (
    GUPSHUP_API_KEY,
    GUPSHUP_SOURCE,
    GUPSHUP_URL,
    APP_NAME,
    aget_gupshup_config,
    asend_gupshup_template_generic,
    normalize_phone,
)

__all__ = [
    "GUPSHUP_API_KEY",
    "GUPSHUP_SOURCE",
    "GUPSHUP_URL",
    "APP_NAME",
    "aget_gupshup_config",
    "asend_gupshup_template_generic",
    "asend_shiprocket_gupshup_template_generic",
    "normalize_phone",
]


async def asend_shiprocket_gupshup_template_generic(
    destination_phone: Any,
    template_id: str,
    params: List[str],
    image_url: Optional[str] = None,
    client_id: Optional[str] = None,
    event_key: Optional[str] = None,
    template_name: Optional[str] = None,
) -> Optional[Any]:
    """Deprecated alias for :func:`asend_gupshup_template_generic`.

    Kept so legacy imports (and any external code references) keep working.
    New code should import from ``fashion_bot.shipping.webhook.
    gupshup_template_sender`` directly and pass ``log_tag="SHIPROCKET"``.
    """
    return await asend_gupshup_template_generic(
        destination_phone,
        template_id,
        params,
        image_url=image_url,
        client_id=client_id,
        event_key=event_key,
        template_name=template_name,
        log_tag="SHIPROCKET",
    )
