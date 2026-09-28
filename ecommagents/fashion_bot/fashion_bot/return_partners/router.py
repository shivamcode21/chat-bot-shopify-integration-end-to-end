"""Client-aware return partner resolution."""

from __future__ import annotations

import logging
from typing import Any

from fashion_bot.env_loader import get_env
from fashion_bot.return_partners.interfaces import ReturnPartnerService
from fashion_bot.return_partners.registry import get_return_partner

logger = logging.getLogger(__name__)


async def _aget_json_config_safe(config_key: str, client_id: str | None) -> dict:
    try:
        from fashion_bot.config_manager import aget_json_config

        return await aget_json_config(config_key, client_id=client_id) or {}
    except Exception as exc:
        logger.debug(
            "[RETURN_PARTNERS] config lookup failed key=%s client_id=%s: %s",
            config_key,
            client_id,
            exc,
        )
        return {}


def _normalize_partner_name(name: Any) -> str:
    return str(name or "").strip().lower()


class ReturnPartnerRouter:
    """Resolve the return partner for a tenant/request."""

    @staticmethod
    async def aresolve_partner_name(
        *,
        client_id: str | None,
        state: dict | None = None,
        partner: str | None = None,
    ) -> str | None:
        explicit = _normalize_partner_name(partner)
        if explicit:
            return explicit

        client_id = client_id or str((state or {}).get("client_id") or "").strip() or None
        details = await _aget_json_config_safe("return_partner_details", client_id)

        configured = _normalize_partner_name(details.get("primary_return_partner"))
        if configured:
            return configured

        rules = await _aget_json_config_safe("return_exchange_rules", client_id)
        configured = _normalize_partner_name(
            rules.get("primary_return_partner")
            or (rules.get("partner") if isinstance(rules, dict) else None)
        )
        if configured:
            return configured

        partners = details.get("return_partners")
        if isinstance(partners, list):
            connected = [
                item
                for item in partners
                if isinstance(item, dict) and item.get("connected", True)
            ]
            connected.sort(key=lambda item: int(item.get("priority") or 9999))
            for item in connected:
                name = _normalize_partner_name(item.get("name"))
                if name:
                    return name

        return_prime_details = await _aget_json_config_safe("return_prime_details", client_id)
        has_return_prime = (
            return_prime_details.get("x_rp_token")
            or return_prime_details.get("api_token")
            or get_env("RETURN_PRIME_X_RP_TOKEN")
            or get_env("RETURN_PRIME_API_TOKEN")
        )
        if has_return_prime:
            return "return_prime"

        return None

    @staticmethod
    async def aresolve_partner(
        *,
        client_id: str | None,
        state: dict | None = None,
        partner: str | None = None,
    ) -> tuple[str | None, ReturnPartnerService | None]:
        partner_name = await ReturnPartnerRouter.aresolve_partner_name(
            client_id=client_id,
            state=state,
            partner=partner,
        )
        if not partner_name:
            return None, None
        return partner_name, get_return_partner(partner_name)
