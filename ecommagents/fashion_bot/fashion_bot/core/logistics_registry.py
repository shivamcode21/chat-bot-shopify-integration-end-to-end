"""
Central registry for delivery-partner integrations.

This is the single dispatch point used by ``ServiceFactory``, the
``LogisticsRouter``, the orchestrator, and the order-editing module so that
adding a new partner (e.g. BlueDart) does not require editing those modules.

To add a new partner:
  1. Create a ``fashion_bot/<partner>/`` package with the usual
     ``tools/``, ``processors/``, ``webhook/`` sub-modules.
  2. In ``fashion_bot/<partner>/__init__.py``, call ``register_partner(...)``.
  3. Append the dotted module path to ``_PARTNER_MODULES`` below so the
     registry imports it at startup.

That's it. ``factory.py``, ``orchestrator.py``, and ``order_editing_graphql.py``
do not need to be edited.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fashion_bot.interfaces.logistics import LogisticsInterface
from fashion_bot.interfaces.order import OrderInterface
from fashion_bot.interfaces.processor import OrderProcessorInterface

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PartnerRegistration:
    """All the wiring needed to integrate a new delivery partner.

    Fields:
        name: canonical, lower-case partner identifier (e.g. ``"shiprocket"``).
        async_logistics_factory: ``await fn(client_id) -> LogisticsInterface``.
        sync_logistics_factory: ``fn(client_id) -> LogisticsInterface`` (lazy).
        order_adapter_factory: ``fn(client_id) -> OrderInterface``.
        processor_factory: ``fn() -> OrderProcessorInterface``.
        webhook_router: FastAPI ``APIRouter`` exposing the partner's inbound
            webhook routes (or ``None`` if the partner has none).
        cancel_recreate_handler: optional callable used by Shopify's
            order-editing flow to cancel-and-recreate on size change. Must
            accept ``order_id, shopify_order_data, old_size, new_size,
            state`` keyword args and return a result dict.
        enricher_type: optional string key used by ``VendorConfigManager``
            enrichment pipelines (e.g. ``"shiprocket_status"``).
        enricher_factory: optional factory for the enricher class.
        order_search_result_key: optional key under which this partner's
            adapter wraps its bulk search results (e.g. Shiprocket's
            ``{"orders": [...]}`` envelope). The orchestrator uses this to
            unwrap the first record without hard-coding partner names.
        default_update_strategy: optional order-update strategy used when a
            tenant has NOT configured one for this partner (one of
            ``"update_inplace"``, ``"escalate_for_manual_update"``,
            ``"cancel_and_recreate"``). Lets the *partner type* drive the
            default — a partner with a full edit API (Shiprocket) declares
            ``"update_inplace"`` so unconfigured tenants are not spammed with
            manual-sync escalations, while a limited-API partner (Delhivery)
            declares ``"escalate_for_manual_update"``. ``None`` falls back to
            the global safe default in ``config_manager``.
    """
    name: str
    async_logistics_factory: Callable[[Optional[str]], Awaitable[LogisticsInterface]]
    sync_logistics_factory: Callable[[Optional[str]], LogisticsInterface]
    order_adapter_factory: Callable[[Optional[str]], OrderInterface]
    processor_factory: Callable[[], OrderProcessorInterface]
    webhook_router: Optional[Any] = None
    cancel_recreate_handler: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None
    enricher_type: Optional[str] = None
    enricher_factory: Optional[Callable[[], Any]] = None
    order_search_result_key: Optional[str] = None
    default_update_strategy: Optional[str] = None


# ─── In-memory registry ──────────────────────────────────────────────

_REGISTRY: Dict[str, PartnerRegistration] = {}


def register_partner(reg: PartnerRegistration) -> None:
    """Register a delivery-partner integration.

    Idempotent: re-registering an identical ``PartnerRegistration`` is a
    no-op so module import order does not matter.
    """
    key = (reg.name or "").lower()
    if not key:
        raise ValueError("PartnerRegistration.name must be a non-empty string")
    existing = _REGISTRY.get(key)
    if existing is not None and existing != reg:
        logger.warning(
            f"[REGISTRY] partner {key!r} re-registered with different config; "
            f"keeping the first registration"
        )
        return
    _REGISTRY[key] = reg
    logger.debug(f"[REGISTRY] registered partner {key!r}")


def get_partner(name: Optional[str]) -> Optional[PartnerRegistration]:
    """Look up a partner registration by canonical name (case-insensitive)."""
    if not name:
        return None
    return _REGISTRY.get(name.lower())


def all_partners() -> Dict[str, PartnerRegistration]:
    """Return a copy of every registered partner, keyed by canonical name."""
    return dict(_REGISTRY)


def integrated_partner_names() -> List[str]:
    """List the canonical names of every integrated partner."""
    return list(_REGISTRY.keys())


def is_integrated(name: Optional[str]) -> bool:
    """True iff there is a registered adapter for ``name``."""
    return bool(name) and name.lower() in _REGISTRY


# ─── Auto-discovery of partner packages ───────────────────────────────

# Dotted module paths that should be imported at startup so each partner's
# ``__init__.py`` can call ``register_partner(...)``. Adding a new partner
# means appending one line here.
_PARTNER_MODULES: List[str] = [
    "fashion_bot.shiprocket",
    "fashion_bot.delhivery",
    "fashion_bot.clickpost",
]


def _autodiscover() -> None:
    """Import every partner module so its registration side-effect runs."""
    for module_name in _PARTNER_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover — protect startup
            logger.error(
                f"[REGISTRY] failed to import partner module {module_name!r}: {exc}",
                exc_info=True,
            )


# Trigger discovery once at module-load time. This is safe because the
# partner packages only register; they don't construct adapters.
_autodiscover()
