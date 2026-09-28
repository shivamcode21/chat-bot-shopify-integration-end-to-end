"""Per-client WhatsApp API version resolution.

The platform supports more than one WhatsApp send transport:

* ``legacy``     – the original Gupshup ``api.gupshup.io/wa/api/v1`` integration
                   (the existing ``send_message`` / ``asend_gupshup_template_generic``
                   code paths). This is the default when nothing is configured.
* ``enterprise`` – the newer Gupshup "WhatsApp API" GatewayAPI
                   (``mediaapi.smsgupshup.com/GatewayAPI/rest``, Template-ID based,
                   userid + Bearer secret). See
                   ``design_docs/whatspp_enterprise.txt``. Routed to
                   :mod:`fashion_bot.utils.whatsapp_enterprise_client`.

Resolution reads the ``whatsapp_api_version`` client config through the
three-tier cache (memory → Redis → DB, AGENTS.md §3). A missing value or any
error fails *open* to ``legacy`` so existing tenants keep using the current
code unchanged.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

WHATSAPP_API_VERSION_LEGACY = "legacy"
WHATSAPP_API_VERSION_ENTERPRISE = "enterprise"

# Config key in ``client_configs`` holding the version selector. The stored
# value may be a bare string ("enterprise") or a JSON object ({"version": ...}).
WHATSAPP_API_VERSION_CONFIG_KEY = "whatsapp_api_version"

# Aliases that all map onto the enterprise transport.
_ENTERPRISE_ALIASES = {
    WHATSAPP_API_VERSION_ENTERPRISE,
    "v2",
    "gupshup_enterprise",
    "smsgupshup",
    "gateway",
    "new",
}


def normalize_whatsapp_api_version(raw: object) -> str:
    """Map a raw stored value onto a canonical version string.

    Returns ``WHATSAPP_API_VERSION_ENTERPRISE`` for any recognised enterprise
    alias, otherwise ``WHATSAPP_API_VERSION_LEGACY``.
    """
    value = raw
    if isinstance(value, dict):
        value = value.get("version") or value.get("whatsapp_api_version")
    if not isinstance(value, str):
        return WHATSAPP_API_VERSION_LEGACY
    return (
        WHATSAPP_API_VERSION_ENTERPRISE
        if value.strip().lower() in _ENTERPRISE_ALIASES
        else WHATSAPP_API_VERSION_LEGACY
    )


async def aget_whatsapp_api_version(
    client_id: Optional[str], trace_id: Optional[str] = None
) -> str:
    """Resolve the WhatsApp API version for ``client_id`` (tiered-cached).

    Fail-open: with no ``client_id``, no config row, or on any error we return
    ``legacy`` so the existing send code path runs unchanged.
    """
    if not client_id:
        return WHATSAPP_API_VERSION_LEGACY
    try:
        # Imported lazily to avoid a module-load cycle (config_manager imports
        # several utils modules at import time).
        from fashion_bot.config_manager import aget_config

        raw = await aget_config(
            WHATSAPP_API_VERSION_CONFIG_KEY, client_id=client_id
        )
        version = normalize_whatsapp_api_version(raw)
        if version == WHATSAPP_API_VERSION_ENTERPRISE:
            logger.info(
                "[WHATSAPP_VERSION] client_id=%s resolved=enterprise trace_id=%s",
                client_id,
                trace_id,
            )
        return version
    except Exception as exc:  # noqa: BLE001 - fail-open to legacy
        logger.warning(
            "[WHATSAPP_VERSION] resolution failed client_id=%s trace_id=%s err=%s; "
            "defaulting to legacy",
            client_id,
            trace_id,
            exc,
        )
        return WHATSAPP_API_VERSION_LEGACY
