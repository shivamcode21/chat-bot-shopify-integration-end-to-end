"""ClickPost vendor package -- live partner registration.

Registers a ClickPost logistics + order adapter pair with the central
``logistics_registry``. A tenant holding real ``clickpost_details``
credentials is routed to ClickPost and its adapters issue live API calls,
so changes here affect production shipments.

Enabling a tenant is a configuration change, not a code change: store real
``client_configs.clickpost_details`` credentials and mark the integration
connected. Until then every adapter method returns ``configuration_missing``
rather than touching the network, so an unconfigured tenant -- or one still
holding placeholder values -- stays inert (see
``logistics_adapter.ClickPostLogisticsAdapter._validate_config``).

Two facts about ClickPost shape the design:

  - It is an aggregator, so Shopify records the *underlying courier* as the
    ``tracking_company`` -- never "clickpost". Partner resolution therefore
    keys off the tenant-specific tracking URL host, which is unambiguous,
    rather than the courier-name alias table, which is global and already
    maps those courier names elsewhere.
  - A shipment is identified by the pair (waybill, cp_id), not the waybill
    alone. Shopify has no field for ``cp_id``, so it is recovered from that
    same tracking URL.

Deliberately not registered: no webhook router, so status is polled; and no
cancel-and-recreate handler, so a size change on a ClickPost shipment is
committed in Shopify and escalated for a manual courier sync rather than
being applied against the carrier here.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from fashion_bot.core.logistics_registry import (
        PartnerRegistration,
        register_partner,
    )
    from fashion_bot.clickpost.tools.logistics_adapter import ClickPostLogisticsAdapter
    from fashion_bot.clickpost.tools.order_adapter import ClickPostOrderAdapter
    from fashion_bot.clickpost.processors.order_processor import ClickPostOrderProcessor

    async def _aget_clickpost_adapter(client_id):
        return await ClickPostLogisticsAdapter.create(client_id=client_id)

    def _get_clickpost_adapter(client_id):
        return ClickPostLogisticsAdapter(client_id=client_id)

    def _get_clickpost_order_adapter(client_id):
        return ClickPostOrderAdapter(client_id=client_id)

    register_partner(
        PartnerRegistration(
            name="clickpost",
            async_logistics_factory=_aget_clickpost_adapter,
            sync_logistics_factory=_get_clickpost_adapter,
            order_adapter_factory=_get_clickpost_order_adapter,
            processor_factory=ClickPostOrderProcessor,
            webhook_router=None,
            cancel_recreate_handler=None,
            enricher_type=None,
            enricher_factory=None,
            order_search_result_key=None,
            # ClickPost publishes no endpoint for editing a manifested
            # shipment, so unconfigured tenants escalate for manual sync
            # rather than silently failing an inplace edit.
            default_update_strategy="escalate_for_manual_update",
        )
    )
except Exception as exc:  # pragma: no cover
    logger.error(f"[REGISTRY] ClickPost partner registration failed: {exc}", exc_info=True)
