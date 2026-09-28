"""
Optional per-client WebSocket Origin allowlist (defense in depth).

client_configs config_key: ``widget_allowed_origins``
Value: JSON array of full origins, e.g. ``["https://www.example.com","https://example.com"]``.

If the list is missing or empty, no Origin check is applied (allow any browser origin).
If non-empty, the connection is allowed when either:
1) WebSocket ``Origin`` header matches an allowed origin, or
2) ``embed_parent_origin`` query parameter matches an allowed origin.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from fashion_bot.config_manager import aget_config
from fashion_bot.env_loader import get_bool

logger = logging.getLogger(__name__)

CONFIG_KEY = "widget_allowed_origins"
ENABLE_ORIGIN_ALLOWLIST_ENV = "ENABLE_WIDGET_ORIGIN_ALLOWLIST"


def _normalize_origin(o: str) -> str:
    return (o or "").strip().rstrip("/")


def _parse_origins(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [_normalize_origin(str(x)) for x in raw if x]
    if isinstance(raw, str):
        try:
            j = json.loads(raw)
            if isinstance(j, list):
                return [_normalize_origin(str(x)) for x in j if x]
        except json.JSONDecodeError:
            pass
    return []


def _origin_allowlist_enabled() -> bool:
    return get_bool(ENABLE_ORIGIN_ALLOWLIST_ENV, True)


async def verify_embed_origin_for_client(client_id: str, origin: Optional[str]) -> bool:
    """
    If widget_allowed_origins is empty, allow.
    Otherwise require ``origin`` to match one allowed origin.
    """
    if not _origin_allowlist_enabled():
        return True
    if not client_id:
        return False
    raw = await aget_config(CONFIG_KEY, client_id=client_id)
    allowed = _parse_origins(raw)
    if not allowed:
        return True
    if not origin:
        logger.warning("Origin required by widget_allowed_origins but missing for client %s", client_id[:8])
        return False
    cand = _normalize_origin(origin)
    ok = cand in allowed
    if not ok:
        logger.warning(
            "Origin %s not in allowed list for client %s",
            cand,
            client_id[:8],
        )
    return ok


async def verify_websocket_embed_for_client(
    client_id: str,
    origin: Optional[str],
    embed_parent_origin: Optional[str],
) -> bool:
    """
    Validate allowlist using either WS ``Origin`` or parent storefront origin.
    """
    if not _origin_allowlist_enabled():
        return True
    if not client_id:
        return False
    raw = await aget_config(CONFIG_KEY, client_id=client_id)
    allowed = _parse_origins(raw)
    if not allowed:
        return True

    ws_origin = _normalize_origin(origin or "")
    parent_origin = _normalize_origin(embed_parent_origin or "")
    ok = (ws_origin and ws_origin in allowed) or (parent_origin and parent_origin in allowed)
    if not ok:
        logger.warning(
            "Origin check failed for client %s ws_origin=%s embed_parent_origin=%s",
            client_id[:8],
            ws_origin or None,
            parent_origin or None,
        )
    return ok
