"""
ServiceFactory — dispatches to the right adapter / processor / enricher
based on vendor name. Logistics partners are looked up through the central
``logistics_registry`` so adding a new partner (e.g. BlueDart) requires no
edits to this file.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

from fashion_bot.interfaces.enricher import OrderEnricherInterface
from fashion_bot.interfaces.logistics import LogisticsInterface
from fashion_bot.interfaces.order import OrderInterface
from fashion_bot.interfaces.processor import OrderProcessorInterface
from fashion_bot.interfaces.product import ProductInterface

# Importing the registry triggers `_autodiscover` which imports every
# partner sub-package, each of which self-registers via
# `register_partner(...)`. After this import, ``logistics_registry`` knows
# about every integrated partner.
from fashion_bot.core import logistics_registry
from fashion_bot.core.vendor_config import VendorConfigManager
from fashion_bot.mock.processors.order_processor import MockOrderProcessor
from fashion_bot.mock.tools.logistics_adapter import MockLogisticsAdapter
from fashion_bot.mock.tools.order_adapter import MockOrderAdapter
from fashion_bot.mock.tools.product_adapter import MockProductAdapter
from fashion_bot.shopify.processors.order_processor import ShopifyOrderProcessor
from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter
from fashion_bot.shopify.tools.product_adapter import ShopifyProductAdapter
from fashion_bot.client_context import get_client_id

# Factory logger for debugging routes
logger = logging.getLogger("fashion_bot.factory")


def _log_factory_route(service_type: str, adapter_class: str, is_mock: bool, extra: str = ""):
    """Log factory routing decisions for debugging."""
    mode = "MOCK" if is_mock else "PROD"
    msg = f"[FACTORY] {mode} {service_type}→{adapter_class}"
    if extra:
        msg += f" | {extra}"
    logger.debug(msg)


def _is_mock_mode() -> bool:
    return os.getenv("USE_MOCK_SERVICES", "").lower() == "true"


async def _aresolve_logistics_partner(
    vendor: Optional[str],
    client_id: Optional[str],
    state: Optional[Dict],
) -> str:
    """Return the canonical name of the partner to use for a logistics request.

    Resolution order:
      1. Explicit ``vendor`` arg (fast path).
      2. ``state.order_dto`` carrier — URL-first (``tracking_url`` is the
         authoritative platform signal; ``tracking_company`` is the fallback),
         resolved the SAME way the LogisticsRouter and orchestrator do so the
         factory never picks a different partner than the rest of the pipeline
         for the same order.
      3. First connected integrated partner from
         ``delivery_partner_integrations`` (priority ASC, created_at DESC).
      4. Hard fallback to ``"shiprocket"`` to preserve prior behaviour for
         tenants without DB rows.
    """
    if vendor:
        return vendor.lower()

    try:
        from fashion_bot.utils.delivery_partner_utils import (
            aget_integrated_partners,
            aresolve_effective_partner_for_order,
        )

        order_dto = None
        if state and isinstance(state.get("order_dto"), dict):
            order_dto = state["order_dto"]
        if order_dto:
            # URL-first resolution (tracking_url → canonical, then
            # tracking_company alias fallback). Without this an order shipped
            # via Shiprocket but carrying tracking_company='Delhivery' would
            # mis-resolve to the Delhivery adapter on a bare factory call.
            canonical, is_integrated = await aresolve_effective_partner_for_order(
                order_dto, state=state,
            )
            if is_integrated and canonical:
                return canonical

        connected = await aget_integrated_partners(client_id=client_id, state=state)
        if connected:
            return connected[0]
    except Exception as exc:
        logger.warning(f"[FACTORY] partner auto-detect failed for {client_id}: {exc}")

    # No partner could be resolved from the order carrier or the client's
    # delivery_partner_integrations rows. Fall back to Shiprocket for backward
    # compatibility, but log the true cause so a downstream "Shiprocket is not
    # configured" error isn't mistaken for the client actually being on Shiprocket.
    logger.warning(
        f"[FACTORY] no connected logistics partner for client {client_id}; defaulting to shiprocket"
    )
    return "shiprocket"


def _sync_resolve_logistics_partner(vendor: Optional[str]) -> str:
    """Sync variant — no DB lookup. Falls back to Shiprocket if vendor is empty."""
    return (vendor or "shiprocket").lower()


class ServiceFactory:
    # ── async factory methods (preferred – eagerly loads config) ──────────

    @staticmethod
    async def aget_product_service(
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        vendor: Optional[str] = None,
    ) -> ProductInterface:
        """Async factory that returns adapter with config pre-loaded."""
        if not client_id and state:
            client_id = state.get("client_id")
        if not vendor:
            vendor = VendorConfigManager.get_primary_vendor(client_id=client_id, state=state)
        if _is_mock_mode():
            _log_factory_route(
                "ProductService", "MockProductAdapter", True,
                f"vendor={vendor}, client_id={client_id}",
            )
            return MockProductAdapter(client_id=client_id)
        _log_factory_route(
            "ProductService", "ShopifyProductAdapter", False,
            f"vendor={vendor}, client_id={client_id}",
        )
        return await ShopifyProductAdapter.create(client_id=client_id)

    @staticmethod
    async def aget_order_service(
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        vendor: str = "shopify",
    ) -> OrderInterface:
        """Async factory that returns adapter with config pre-loaded.

        Order service routing is registry-driven for delivery partners and
        delegated to Shopify by default.
        """
        if not client_id and state:
            client_id = state.get("client_id")
        if _is_mock_mode():
            _log_factory_route(
                "OrderService", "MockOrderAdapter", True,
                f"vendor={vendor}, client_id={client_id}",
            )
            return MockOrderAdapter(client_id=client_id)

        if vendor == "shopify":
            _log_factory_route(
                "OrderService", "ShopifyOrderAdapter", False,
                f"vendor={vendor}, client_id={client_id}",
            )
            return await ShopifyOrderAdapter.create(client_id=client_id)

        # Delivery-partner order adapters come from the registry.
        partner_reg = logistics_registry.get_partner(vendor)
        if partner_reg is not None:
            _log_factory_route(
                "OrderService",
                partner_reg.order_adapter_factory.__name__,
                False,
                f"vendor={vendor}, client_id={client_id}",
            )
            return partner_reg.order_adapter_factory(client_id)

        _log_factory_route(
            "OrderService", "ShopifyOrderAdapter", False,
            f"vendor={vendor} (unknown, defaulting), client_id={client_id}",
        )
        return await ShopifyOrderAdapter.create(client_id=client_id)

    @staticmethod
    async def aget_logistics_service(
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        vendor: Optional[str] = None,
    ) -> LogisticsInterface:
        """Async factory that returns a logistics adapter with config pre-loaded.

        Looks the partner up in the central ``logistics_registry``. Adding a
        new partner does not require an edit here.
        """
        if not client_id and state:
            client_id = state.get("client_id")
        # Web/streaming turns carry client_id in the request-scoped ContextVar
        # (set in websocket_chat / streaming_service) but not always in the
        # graph ``state`` dict that reaches LangChain tools. Without this
        # fallback the adapter is built with client_id=None, the Shiprocket
        # config lookup misses, and delivery estimates fail with "Shiprocket
        # is not configured for client None". See client_context docstring.
        if not client_id:
            client_id = get_client_id()
        if _is_mock_mode():
            _log_factory_route(
                "LogisticsService", "MockLogisticsAdapter", True,
                f"client_id={client_id}, vendor={vendor}",
            )
            return MockLogisticsAdapter(client_id=client_id)

        partner = await _aresolve_logistics_partner(vendor, client_id, state)
        partner_reg = logistics_registry.get_partner(partner)
        if partner_reg is None:
            # Shouldn't normally happen — shiprocket is always registered —
            # but degrade gracefully so we never raise from the factory.
            partner_reg = logistics_registry.get_partner("shiprocket")
            if partner_reg is None:
                raise RuntimeError(
                    f"No logistics adapter registered (requested vendor={partner!r})"
                )
            logger.warning(
                f"[FACTORY] no adapter for {partner!r}; falling back to shiprocket"
            )
        _log_factory_route(
            "LogisticsService",
            partner_reg.async_logistics_factory.__name__,
            False,
            f"client_id={client_id}, partner={partner_reg.name}",
        )
        return await partner_reg.async_logistics_factory(client_id)

    # ── sync factory methods (legacy – lazy config on first method call) ─

    @staticmethod
    def get_product_service(
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        vendor: Optional[str] = None,
    ) -> ProductInterface:
        """Sync fallback. Config loaded lazily on first adapter method call."""
        if not client_id and state:
            client_id = state.get("client_id")
        if not vendor:
            vendor = VendorConfigManager.get_primary_vendor(client_id=client_id, state=state)
        if _is_mock_mode():
            _log_factory_route(
                "ProductService", "MockProductAdapter", True,
                f"vendor={vendor}, client_id={client_id}",
            )
            return MockProductAdapter(client_id=client_id)
        _log_factory_route(
            "ProductService", "ShopifyProductAdapter", False,
            f"vendor={vendor}, client_id={client_id}",
        )
        return ShopifyProductAdapter(client_id=client_id)

    @staticmethod
    def get_order_service(
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        vendor: str = "shopify",
    ) -> OrderInterface:
        """Sync fallback. Config loaded lazily on first adapter method call."""
        if not client_id and state:
            client_id = state.get("client_id")
        if _is_mock_mode():
            _log_factory_route(
                "OrderService", "MockOrderAdapter", True,
                f"vendor={vendor}, client_id={client_id}",
            )
            return MockOrderAdapter(client_id=client_id)

        if vendor == "shopify":
            _log_factory_route(
                "OrderService", "ShopifyOrderAdapter", False,
                f"vendor={vendor}, client_id={client_id}",
            )
            return ShopifyOrderAdapter(client_id=client_id)

        partner_reg = logistics_registry.get_partner(vendor)
        if partner_reg is not None:
            _log_factory_route(
                "OrderService",
                partner_reg.order_adapter_factory.__name__,
                False,
                f"vendor={vendor}, client_id={client_id}",
            )
            return partner_reg.order_adapter_factory(client_id)

        _log_factory_route(
            "OrderService", "ShopifyOrderAdapter", False,
            f"vendor={vendor} (unknown, defaulting), client_id={client_id}",
        )
        return ShopifyOrderAdapter(client_id=client_id)

    @staticmethod
    def get_logistics_service(
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        vendor: Optional[str] = None,
    ) -> LogisticsInterface:
        """Sync fallback. Config loaded lazily on first adapter method call.

        Note: this path does *not* perform DB-backed auto-detect; callers
        relying on multi-partner routing should use the async variant.
        """
        if not client_id and state:
            client_id = state.get("client_id")
        if not client_id:
            client_id = get_client_id()
        if _is_mock_mode():
            _log_factory_route(
                "LogisticsService", "MockLogisticsAdapter", True,
                f"client_id={client_id}, vendor={vendor}",
            )
            return MockLogisticsAdapter(client_id=client_id)

        partner = _sync_resolve_logistics_partner(vendor)
        partner_reg = logistics_registry.get_partner(partner)
        if partner_reg is None:
            partner_reg = logistics_registry.get_partner("shiprocket")
            if partner_reg is None:
                raise RuntimeError(
                    f"No logistics adapter registered (requested vendor={partner!r})"
                )
            logger.warning(
                f"[FACTORY] no sync adapter for {partner!r}; falling back to shiprocket"
            )
        _log_factory_route(
            "LogisticsService",
            partner_reg.sync_logistics_factory.__name__,
            False,
            f"client_id={client_id}, partner={partner_reg.name}",
        )
        return partner_reg.sync_logistics_factory(client_id)

    # ── non-adapter factories (unchanged) ────────────────────────────────

    @staticmethod
    def get_order_processor(
        vendor: str = "shopify",
        state: Optional[Dict] = None,
    ) -> OrderProcessorInterface:
        """Get the appropriate OrderProcessorInterface implementation.
        Supports mock mode via USE_MOCK_SERVICES environment variable.
        """
        client_id = state.get("client_id") if state else None

        if _is_mock_mode():
            _log_factory_route(
                "OrderProcessor", "MockOrderProcessor", True,
                f"vendor={vendor}, client_id={client_id}",
            )
            return MockOrderProcessor(client_id=client_id)

        partner_reg = logistics_registry.get_partner(vendor)
        if partner_reg is not None:
            _log_factory_route(
                "OrderProcessor",
                partner_reg.processor_factory.__name__,
                False,
                f"vendor={vendor}",
            )
            return partner_reg.processor_factory()

        _log_factory_route("OrderProcessor", "ShopifyOrderProcessor", False, f"vendor={vendor}")
        return ShopifyOrderProcessor()

    @staticmethod
    def get_primary_vendor(state: Optional[Dict] = None, client_id: Optional[str] = None) -> str:
        """Determine the primary vendor for the current context."""
        return VendorConfigManager.get_primary_vendor(client_id=client_id, state=state)

    @staticmethod
    def get_enrichment_pipeline(
        state: Optional[Dict] = None,
        client_id: Optional[str] = None,
    ) -> List[OrderEnricherInterface]:
        """Returns the list of enrichers (Topology) for the given client config."""
        pipeline_config = VendorConfigManager.get_enrichment_pipeline_config(
            client_id=client_id, state=state,
        )
        enrichers = []
        for step in pipeline_config:
            enricher = ServiceFactory._create_enricher(step.enricher_type, step.config)
            if enricher:
                enrichers.append(enricher)
        return enrichers

    @staticmethod
    def _create_enricher(enricher_type: str, config: Dict) -> Optional[OrderEnricherInterface]:
        """Factory method to create enricher instances based on type.
        Maps enricher_type string to actual enricher class. Mock mode supported.
        """
        is_mock = _is_mock_mode()
        if is_mock:
            from fashion_bot.mock.enrichers.mock_enricher import (
                MockGraphQLEnricher,
                MockStatusEnricher,
                MockTrackingEnricher,
            )

            if enricher_type in ["shopify_graphql", "mock_graphql"]:
                _log_factory_route("Enricher", "MockGraphQLEnricher", True, f"type={enricher_type}")
                return MockGraphQLEnricher()
            if enricher_type in ["shopify_tracking", "mock_tracking"]:
                _log_factory_route("Enricher", "MockTrackingEnricher", True, f"type={enricher_type}")
                return MockTrackingEnricher()
            if enricher_type in ["shiprocket_status", "mock_status"] or enricher_type.endswith("_status"):
                _log_factory_route("Enricher", "MockStatusEnricher", True, f"type={enricher_type}")
                return MockStatusEnricher()

            _log_factory_route("Enricher", "MockStatusEnricher", True, f"type={enricher_type} (default)")
            return MockStatusEnricher()

        # Production enrichers — Shopify enrichers stay hardcoded; partner
        # enrichers come from the registry so a new partner just needs to
        # populate ``PartnerRegistration.enricher_type`` + ``enricher_factory``.
        if enricher_type == "shopify_graphql":
            from fashion_bot.shopify.enrichers.graphql_enricher import ShopifyGraphQLEnricher
            _log_factory_route("Enricher", "ShopifyGraphQLEnricher", False, f"type={enricher_type}")
            return ShopifyGraphQLEnricher()

        if enricher_type == "shopify_tracking":
            from fashion_bot.shopify.enrichers.tracking_enricher import ShopifyTrackingEnricher
            _log_factory_route("Enricher", "ShopifyTrackingEnricher", False, f"type={enricher_type}")
            return ShopifyTrackingEnricher()

        for partner_reg in logistics_registry.all_partners().values():
            if partner_reg.enricher_type == enricher_type and partner_reg.enricher_factory:
                _log_factory_route(
                    "Enricher",
                    partner_reg.enricher_factory.__name__,
                    False,
                    f"type={enricher_type}",
                )
                return partner_reg.enricher_factory()

        _log_factory_route("Enricher", "None", False, f"type={enricher_type} (unknown)")
        return None
