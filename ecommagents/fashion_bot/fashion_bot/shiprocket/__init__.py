"""Shiprocket vendor package.

Implements ``LogisticsInterface`` + ``OrderInterface`` + processor +
webhook for Shiprocket, then registers itself with the central
``logistics_registry`` so ``ServiceFactory`` and the LogisticsRouter can
dispatch by canonical partner name without hard-coding ``"shiprocket"``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Self-register with the central partner registry. Doing this at package
# import time means ``import fashion_bot.shiprocket`` is enough to make the
# adapter callable through the factory.
try:
    from fashion_bot.core.logistics_registry import (
        PartnerRegistration,
        register_partner,
    )
    from fashion_bot.shiprocket.tools.logistics_adapter import ShiprocketLogisticsAdapter
    from fashion_bot.shiprocket.tools.order_adapter import ShiprocketOrderAdapter
    from fashion_bot.shiprocket.processors.order_processor import ShiprocketOrderProcessor

    async def _aget_shiprocket_adapter(client_id):
        return await ShiprocketLogisticsAdapter.create(client_id=client_id)

    def _get_shiprocket_adapter(client_id):
        return ShiprocketLogisticsAdapter(client_id=client_id)

    def _get_shiprocket_order_adapter(client_id):
        return ShiprocketOrderAdapter(client_id=client_id)

    def _get_shiprocket_enricher():
        from fashion_bot.shiprocket.enrichers.status_enricher import ShiprocketStatusEnricher
        return ShiprocketStatusEnricher()

    # Cancel-and-recreate handler — deferred import to avoid pulling in
    # Shopify modules at adapter registration time.
    async def _ashiprocket_cancel_recreate(**kwargs):
        from fashion_bot.shopify.modules.order_editing_graphql import (
            _aupdate_shiprocket_order_size,
        )
        return await _aupdate_shiprocket_order_size(**kwargs)

    # Webhook router — looked up lazily so a broken router import does not
    # crash registration.
    _shiprocket_router = None
    try:
        from fashion_bot.shiprocket.webhook.shiprocket_webhook import (
            router as _shiprocket_router,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(f"[REGISTRY] Shiprocket webhook router unavailable: {exc}")

    register_partner(
        PartnerRegistration(
            name="shiprocket",
            async_logistics_factory=_aget_shiprocket_adapter,
            sync_logistics_factory=_get_shiprocket_adapter,
            order_adapter_factory=_get_shiprocket_order_adapter,
            processor_factory=ShiprocketOrderProcessor,
            webhook_router=_shiprocket_router,
            cancel_recreate_handler=_ashiprocket_cancel_recreate,
            enricher_type="shiprocket_status",
            enricher_factory=_get_shiprocket_enricher,
            order_search_result_key="orders",
            # Shiprocket's edit API works at any order state (pre- and
            # post-AWB), so unconfigured tenants update in place with no
            # manual-sync escalation — matching the pre-multi-partner flow.
            default_update_strategy="update_inplace",
        )
    )
except Exception as exc:  # pragma: no cover — registration must not crash app startup
    logger.error(f"[REGISTRY] Shiprocket partner registration failed: {exc}", exc_info=True)
