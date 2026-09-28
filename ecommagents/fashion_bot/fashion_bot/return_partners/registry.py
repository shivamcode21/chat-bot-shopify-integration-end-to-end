"""Registry for return/exchange partner services."""

from __future__ import annotations

import logging

from fashion_bot.return_partners.interfaces import ReturnPartnerService

logger = logging.getLogger(__name__)

_PARTNERS: dict[str, ReturnPartnerService] = {}
_DEFAULTS_REGISTERED = False


def _normalize_partner_name(name: str | None) -> str:
    return str(name or "").strip().lower()


def register_return_partner(name: str, service: ReturnPartnerService) -> None:
    partner = _normalize_partner_name(name)
    if not partner:
        raise ValueError("Return partner name is required")
    _PARTNERS[partner] = service
    logger.info("[RETURN_PARTNERS] registered partner=%s", partner)


def get_return_partner(name: str | None) -> ReturnPartnerService | None:
    ensure_default_return_partners_registered()
    return _PARTNERS.get(_normalize_partner_name(name))


def all_return_partners() -> dict[str, ReturnPartnerService]:
    ensure_default_return_partners_registered()
    return dict(_PARTNERS)


def ensure_default_return_partners_registered() -> None:
    """Import built-in partner modules so they self-register."""
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return
    _DEFAULTS_REGISTERED = True
    try:
        import fashion_bot.return_prime.service  # noqa: F401
    except Exception as exc:
        logger.warning("[RETURN_PARTNERS] Return Prime registration unavailable: %s", exc)
    try:
        import fashion_bot.return_partners.shopify_service  # noqa: F401
    except Exception as exc:
        logger.warning("[RETURN_PARTNERS] Shopify return registration unavailable: %s", exc)
