"""
Per-client widget API key verification (X-API-Key / query param).

Stores SHA-256 hex of ``f"{pepper}:{client_id}:{plain_key}"`` in client_configs
under config_key ``widget_api_key_sha256`` as JSON ``{"hash": "..."}``.

Env:
- REQUIRE_WIDGET_API_KEY: if true, WebSocket must present a valid key for the resolved client_id.
- WIDGET_API_KEY_PEPPER: secret prefix for hashing (set in production).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

from fashion_bot.config_manager import aget_config
from fashion_bot.env_loader import get_bool, get_env

logger = logging.getLogger(__name__)

CONFIG_KEY_WIDGET_API_HASH = "widget_api_key_sha256"


def compute_widget_api_key_hash(client_id: str, plain_key: str) -> str:
    """Deterministic hash for storage and verification (pepper + client + key)."""
    pepper = get_env("WIDGET_API_KEY_PEPPER") or ""
    msg = f"{pepper}:{client_id}:{plain_key}".encode("utf-8")
    return hashlib.sha256(msg).hexdigest()


async def _load_stored_hash(client_id: str) -> Optional[str]:
    raw: Any = await aget_config(CONFIG_KEY_WIDGET_API_HASH, client_id=client_id)
    if raw is None:
        return None
    if isinstance(raw, dict):
        h = raw.get("hash")
        return str(h) if h else None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("hash"):
                return str(parsed["hash"])
        except json.JSONDecodeError:
            s = raw.strip()
            if len(s) == 64 and all(c in "0123456789abcdef" for c in s.lower()):
                return s.lower()
    return None


async def verify_widget_api_key_for_client(client_id: str, plain_key: Optional[str]) -> bool:
    """
    Returns True if key is valid for client, or if REQUIRE_WIDGET_API_KEY is false.

    When REQUIRE_WIDGET_API_KEY is true and no hash exists in DB for this client, returns False.
    """
    if not get_bool("REQUIRE_WIDGET_API_KEY", False):
        return True
    if not plain_key or not client_id:
        logger.warning("Widget API key required but missing key or client_id")
        return False
    stored = await _load_stored_hash(client_id)
    if not stored:
        logger.warning(
            "REQUIRE_WIDGET_API_KEY set but no %s for client_id=%s",
            CONFIG_KEY_WIDGET_API_HASH,
            client_id[:8],
        )
        return False
    computed = compute_widget_api_key_hash(client_id, plain_key)
    try:
        return hmac.compare_digest(computed.lower(), stored.lower())
    except Exception:
        return False
