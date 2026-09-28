"""Delhivery vendor package.

Provides logistics + order adapters, processor, enricher, and webhook
handler for the Delhivery delivery partner. Mirrors the Shiprocket package
layout and plugs into the same interfaces (``LogisticsInterface``,
``OrderInterface``, ``OrderProcessorInterface``, ``OrderEnricherInterface``).

Self-registers with the central ``logistics_registry`` at import time so
the factory and LogisticsRouter can dispatch by canonical partner name
without any hard-coded ``"delhivery"`` branches.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from fashion_bot.core.logistics_registry import (
        PartnerRegistration,
        register_partner,
    )
    from fashion_bot.delhivery.tools.logistics_adapter import DelhiveryLogisticsAdapter
    from fashion_bot.delhivery.tools.order_adapter import DelhiveryOrderAdapter
    from fashion_bot.delhivery.processors.order_processor import DelhiveryOrderProcessor

    async def _aget_delhivery_adapter(client_id):
        return await DelhiveryLogisticsAdapter.create(client_id=client_id)

    def _get_delhivery_adapter(client_id):
        return DelhiveryLogisticsAdapter(client_id=client_id)

    def _get_delhivery_order_adapter(client_id):
        return DelhiveryOrderAdapter(client_id=client_id)

    def _get_delhivery_enricher():
        from fashion_bot.delhivery.enrichers.status_enricher import DelhiveryStatusEnricher
        return DelhiveryStatusEnricher()

    async def _adelhivery_cancel_recreate(**kwargs):
        from fashion_bot.delhivery.modules.create_delhivery_order import (
            aupdate_delhivery_order_size,
        )
        return await aupdate_delhivery_order_size(**kwargs)

    _delhivery_router = None
    try:
        from fashion_bot.delhivery.webhook import delhivery_webhook_router as _delhivery_router
    except Exception as exc:  # pragma: no cover
        logger.warning(f"[REGISTRY] Delhivery webhook router unavailable: {exc}")

    register_partner(
        PartnerRegistration(
            name="delhivery",
            async_logistics_factory=_aget_delhivery_adapter,
            sync_logistics_factory=_get_delhivery_adapter,
            order_adapter_factory=_get_delhivery_order_adapter,
            processor_factory=DelhiveryOrderProcessor,
            webhook_router=_delhivery_router,
            cancel_recreate_handler=_adelhivery_cancel_recreate,
            enricher_type="delhivery_status",
            enricher_factory=_get_delhivery_enricher,
            # Delhivery's adapter never wraps results in an envelope — its
            # ``aget_matching_orders`` already returns a bare list.
            order_search_result_key=None,
            # Delhivery's /api/p/edit requires a waybill, so pre-AWB orders
            # cannot be edited via API. Unconfigured tenants therefore escalate
            # for manual sync rather than silently failing an inplace edit.
            default_update_strategy="escalate_for_manual_update",
        )
    )
except Exception as exc:  # pragma: no cover
    logger.error(f"[REGISTRY] Delhivery partner registration failed: {exc}", exc_info=True)
