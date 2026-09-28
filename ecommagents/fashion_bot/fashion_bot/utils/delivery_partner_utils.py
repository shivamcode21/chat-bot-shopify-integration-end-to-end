"""
Generic, vendor-agnostic delivery-partner helpers.

Replaces the Shiprocket-specific helpers in `utils/delivery_utils.py` for
multi-partner flows. Used by the orchestrator, the LogisticsRouter, and the
update/cancel tools to:

- decide which canonical partner an order's `tracking_url` / `tracking_company`
  resolves to (URL is the primary signal; tracking_company is the fallback),
- decide whether that partner is one of our integrated adapters,
- list integrated/non-integrated partners that are *connected* for a client.

## Partner resolution priority

1. **tracking_url** (most reliable): The URL domain unambiguously identifies
   the platform that *manages* the shipment. Shiprocket can assign Delhivery
   as the underlying courier and write `tracking_company='Delhivery'` into
   Shopify — even though all API calls (track, update, cancel) go through
   Shiprocket. `resolve_partner_from_url()` in `vendor_config.py` checks
   TRACKING_URL_PATTERNS and returns the canonical name on a substring match.
2. **tracking_company** (fallback): Resolved via `DELIVERY_PARTNER_ALIASES`
   with exact + prefix matching. Used when no URL pattern matches (e.g. new
   or unfulfilled orders where tracking_url is empty).

The set of integrated partners now comes from the central
``logistics_registry`` — adding a partner via ``register_partner(...)``
makes it visible here automatically. ``INTEGRATED_PARTNERS`` is preserved as
a back-compat name that proxies to ``integrated_partner_names()``.

Reads from `delivery_partner_integrations` via the existing TTL-cached
`aget_all_delivery_partners_cached`. Stays read-only (no state mutation) and
honours the AGENTS.md three-tier cache pattern via the underlying helpers.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _integrated_partner_names() -> frozenset:
    """Current set of canonical names of partners we have integrated adapters for."""
    from fashion_bot.core.logistics_registry import integrated_partner_names
    return frozenset(integrated_partner_names())


class _IntegratedPartnersProxy(frozenset):
    """frozenset that reflects the current ``logistics_registry`` state.

    Kept as a back-compat alias for any external code that did
    ``from delivery_partner_utils import INTEGRATED_PARTNERS``. New code
    should call :func:`_integrated_partner_names` (or
    ``logistics_registry.integrated_partner_names()``) directly.
    """

    def __new__(cls):  # type: ignore[override]
        return super().__new__(cls, _integrated_partner_names())

    def __contains__(self, item) -> bool:  # type: ignore[override]
        return item in _integrated_partner_names()


# Back-compat alias. Membership is evaluated against the live registry.
INTEGRATED_PARTNERS = _IntegratedPartnersProxy()


def _normalize_partner_name(name: Optional[str]) -> str:
    return (name or "").strip().lower()


async def aresolve_canonical_partner_for_order(
    tracking_company: Optional[str],
    state: Optional[Dict] = None,
) -> Tuple[Optional[str], bool]:
    """
    Resolve the canonical logistics partner for an order's tracking_company.

    Args:
        tracking_company: The Shopify-side `fulfillments[].tracking_company`
            string, e.g. "Bluedart", "Shiprocket Assigned", "Delhivery".
        state: LangGraph state (for client_id resolution).

    Returns:
        (canonical, is_integrated_and_connected) where:
          - canonical: name of the integrated partner, or None if unknown.
          - is_integrated_and_connected: True iff canonical is in the
            registry *and* the client has it connected in the
            `delivery_partner_integrations` table.

    Tenant-aware aliasing: if the raw tracking_company already matches one
    of the *connected* integrated partners for this tenant, that direct
    integration wins over the alias table. This keeps the carrier-via-
    Shiprocket aliases working for tenants that don't have a direct
    BlueDart integration while routing BlueDart-direct tenants correctly
    when that integration ships.
    """
    from fashion_bot.core.vendor_config import resolve_partner_alias

    raw_name = _normalize_partner_name(tracking_company)
    if not raw_name:
        return None, False

    connected = set(await aget_connected_partners(state=state))
    integrated_names = _integrated_partner_names()

    # 1) Direct integration wins — if the tenant has a *direct* connection
    #    matching the tracking_company string, route to that partner even
    #    if DELIVERY_PARTNER_ALIASES says otherwise (e.g. a future
    #    BlueDart-direct tenant where 'bluedart' is no longer aliased to
    #    Shiprocket).
    if raw_name in connected and raw_name in integrated_names:
        return raw_name, True

    # 2) Otherwise consult the alias table with prefix matching so variants
    #    like "BlueDart Surface 2KG" resolve to "shiprocket".
    canonical = resolve_partner_alias(raw_name)
    if not canonical:
        return None, False

    if canonical not in integrated_names:
        return canonical, False

    return canonical, canonical in connected


async def aget_connected_partners(
    client_id: Optional[str] = None,
    state: Optional[Dict] = None,
) -> List[str]:
    """Return all *connected* partners for a client (lower-cased canonical names)."""
    from fashion_bot.utils.delivery_utils import aget_all_delivery_partners_cached

    # Resolve client_id explicitly — the previous one-liner relied on
    # operator precedence and silently returned [] on missing cid, which
    # AGENTS.md §4.3 forbids (silent WARNING returns hide real tenant
    # misconfiguration).
    cid = client_id
    if not cid and state:
        cid = state.get("client_id")
    if not cid:
        try:
            from fashion_bot.rollbar_config import report_error
            report_error(
                "aget_connected_partners called without resolvable client_id",
                level="error",
                state=state,
            )
        except Exception:
            logger.error("aget_connected_partners: missing client_id and rollbar report failed")
        return []
    try:
        partners = await aget_all_delivery_partners_cached(cid)
        return [
            _normalize_partner_name(p.get("name"))
            for p in partners
            if p.get("connected")
        ]
    except Exception as exc:
        logger.error(f"❌ aget_connected_partners({cid}) failed: {exc}", exc_info=True)
        return []


async def aget_integrated_partners(
    client_id: Optional[str] = None,
    state: Optional[Dict] = None,
) -> List[str]:
    """Return connected partners that we have integrated adapters for."""
    integrated = _integrated_partner_names()
    return [p for p in await aget_connected_partners(client_id, state) if p in integrated]


async def aget_non_integrated_partners(
    client_id: Optional[str] = None,
    state: Optional[Dict] = None,
) -> List[str]:
    """Return connected partners that we do *not* have integrated adapters for."""
    integrated = _integrated_partner_names()
    return [
        p for p in await aget_connected_partners(client_id, state)
        if p not in integrated
    ]


# Deprecated: prefer ``aget_non_integrated_partners``. Kept as a thin alias
# until external callers migrate.
async def aget_non_shiprocket_partners(
    client_id: Optional[str] = None,
    state: Optional[Dict] = None,
) -> List[str]:
    """Deprecated alias for :func:`aget_non_integrated_partners`."""
    return await aget_non_integrated_partners(client_id, state)


async def aresolve_effective_partner_for_order(
    order_dto: Dict,
    state: Optional[Dict] = None,
) -> Tuple[Optional[str], bool]:
    """
    Resolve the canonical logistics partner for an order using URL-first logic.

    Resolution priority:
      1. tracking_url substring match (``resolve_partner_from_url``) — the URL
         domain is the authoritative signal for which platform manages the
         shipment. Shiprocket can dispatch via Delhivery and write
         tracking_company='Delhivery', but the tracking_url will contain
         'shiprocket', correctly identifying Shiprocket as the integration.
      2. tracking_company alias table (``aresolve_canonical_partner_for_order``)
         — fallback for NEW/unfulfilled orders where tracking_url is empty.

    The same precedence matters for aggregators, which never appear as the
    tracking_company at all: the courier they dispatched through is recorded
    there instead, so only the URL identifies the managing platform.

    A URL match is not final. If it names a platform the tenant has not
    connected, resolution falls through to the alias table rather than
    returning a partner that cannot be queried — an aggregator's tracking
    domain may front a courier that a different, connected integration
    manages for this tenant.

    Args:
        order_dto: Order mapping read for ``tracking_url`` and
            ``tracking_company``. Missing or empty keys are treated as absent
            rather than an error.

    Returns:
        ``(canonical, is_integrated_and_connected)`` — the same contract as
        :func:`aresolve_canonical_partner_for_order`, which supplies the
        result whenever the URL does not yield a connected partner.
    """
    from fashion_bot.core.vendor_config import resolve_partner_from_url

    tracking_url = (order_dto or {}).get("tracking_url") or ""
    tracking_company = (order_dto or {}).get("tracking_company") or ""

    url_canonical = resolve_partner_from_url(tracking_url)
    if url_canonical:
        connected = set(await aget_connected_partners(state=state))
        integrated_names = _integrated_partner_names()
        is_integrated_and_connected = (
            url_canonical in integrated_names and url_canonical in connected
        )
        if is_integrated_and_connected:
            return url_canonical, True
        # The URL names a platform this tenant has not connected, so it
        # cannot be queried for them. Fall through to the alias table rather
        # than returning a partner they don't have: an aggregator's tracking
        # domain may front a courier that another integration manages for
        # this tenant, and that integration is still the right one to ask.
    return await aresolve_canonical_partner_for_order(tracking_company, state=state)


async def aget_partners_for_order(
    order_dto: Dict,
    state: Optional[Dict] = None,
) -> List[str]:
    """
    Pick which integrated partners to query for a given order.

    Resolution priority (see module docstring and design doc):
      1. tracking_url substring match — authoritative platform signal.
         e.g. URL contains 'delhivery' → ["delhivery"] even if
         tracking_company is empty or set to a Shiprocket courier name.
      2. tracking_company alias table — fallback for unfulfilled orders
         where tracking_url is not yet set.
      3. Fan-out — if neither resolves, race ALL connected integrated
         partners with per-partner timeouts.

    Args:
        order_dto: Shopify-normalized order DTO (tracking_url + tracking_company).
        state: LangGraph state (for client_id).

    Returns:
        Ordered list of canonical partner names, e.g. ["delhivery"] or
        ["shiprocket", "delhivery"]. Empty list means no integrated partner
        applies (caller should fall back to Shopify-only / escalation path).
    """
    integrated = await aget_integrated_partners(state=state)
    if not integrated:
        return []

    canonical, is_integrated = await aresolve_effective_partner_for_order(
        order_dto,
        state=state,
    )
    if is_integrated and canonical in integrated:
        return [canonical]

    # Unknown / missing carrier → race all connected integrated partners.
    return list(integrated)
