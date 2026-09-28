"""Per-client config for the Return Prime -> WhatsApp/email notification flow.

Both keys live in ``client_configs`` (generic client_id/config_key/config_value
JSONB store — no schema change needed):

* ``return_prime_notifications`` — the feature switch. ``{"enabled": true}``
  turns the flow on for that client; anything missing/false keeps it off.
  Defaults to **disabled** so nothing sends until a client is explicitly
  opted in.
* ``return_exchange_client_info_return_prime`` — the enterprise client's own
  ops/support email(s) that get notified when a return/exchange/refund is
  initiated. Shape: ``{"email": "ops@brand.com"}`` or
  ``{"to": [...], "cc": [...]}``.
* ``return_prime_notifications.auto_process_days`` — optional; the "if no
  action is taken within X days" number quoted in the client-team email.
  Defaults to ``DEFAULT_AUTO_PROCESS_DAYS`` when unset.
* ``return_prime_details.merchant_dashboard_url`` — optional; the client's
  Return Prime *merchant* dashboard link (not the customer-facing return
  portal — that's ``return_prime_details.portal_url`` elsewhere). Reuses the
  existing ``return_prime_details`` config key that already holds
  ``webhook_secret`` / ``x_rp_token`` / ``portal_url``. Left unset, the
  "Action Required" line is omitted from the email rather than guessing a URL.
"""

from __future__ import annotations

from typing import Any, List, Tuple

RETURN_PRIME_NOTIFICATIONS_CONFIG_KEY = "return_prime_notifications"
RETURN_EXCHANGE_CLIENT_INFO_CONFIG_KEY = "return_exchange_client_info_return_prime"
RETURN_PRIME_DETAILS_CONFIG_KEY = "return_prime_details"

DEFAULT_AUTO_PROCESS_DAYS = 3

_TRUTHY = {"1", "true", "yes", "on", "enabled"}


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return False


def _as_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    return []


async def aget_return_prime_notifications_enabled(client_id: str | None) -> bool:
    """Whether the return/exchange/refund notification flow is switched on."""
    if not client_id:
        return False
    from fashion_bot.config_manager import aget_json_config

    cfg = await aget_json_config(RETURN_PRIME_NOTIFICATIONS_CONFIG_KEY, client_id=client_id)
    return _is_truthy((cfg or {}).get("enabled"))


async def aget_return_exchange_client_emails(client_id: str | None) -> Tuple[List[str], List[str]]:
    """Return (to, cc) email addresses configured for the client's own team."""
    if not client_id:
        return [], []
    from fashion_bot.config_manager import aget_json_config

    cfg = await aget_json_config(RETURN_EXCHANGE_CLIENT_INFO_CONFIG_KEY, client_id=client_id) or {}
    to = _as_list(cfg.get("to") or cfg.get("email") or cfg.get("emails"))
    cc = _as_list(cfg.get("cc"))
    return to, cc


async def aget_return_prime_auto_process_days(client_id: str | None) -> int:
    """"If no action is taken within X days" — configurable per client."""
    if not client_id:
        return DEFAULT_AUTO_PROCESS_DAYS
    from fashion_bot.config_manager import aget_json_config

    cfg = await aget_json_config(RETURN_PRIME_NOTIFICATIONS_CONFIG_KEY, client_id=client_id) or {}
    try:
        return int(cfg.get("auto_process_days") or DEFAULT_AUTO_PROCESS_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_PROCESS_DAYS


async def aget_return_prime_merchant_dashboard_url(client_id: str | None) -> str | None:
    """The client's Return Prime *merchant* dashboard link, if configured."""
    if not client_id:
        return None
    from fashion_bot.config_manager import aget_json_config

    cfg = await aget_json_config(RETURN_PRIME_DETAILS_CONFIG_KEY, client_id=client_id) or {}
    url = str(cfg.get("merchant_dashboard_url") or "").strip()
    return url or None
