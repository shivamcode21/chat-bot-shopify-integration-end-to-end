"""
ClickPost logistics adapter -- implements LogisticsInterface for the
ClickPost (https://www.clickpost.in/) shipping-aggregator partner.

These calls are live: for a tenant configured with real credentials,
tracking reads and cancellations are issued against its actual shipments.
Every public method validates its config first via ``_validate_config`` /
``_is_dummy_value`` and returns a safe ``configuration_missing`` result
instead of touching the network when the tenant config is absent or still
holds placeholder values.

Endpoints below are taken from ClickPost's published API reference
(https://clickpost.github.io/slate/) and span three hosts -- the order and
cancel APIs, tracking reads, and the predicted-SLA model each sit on their
own base URL.

ClickPost identifies a shipment by the pair (waybill, cp_id) rather than the
waybill alone; ``cp_id`` names the courier actually carrying the parcel and
is required by both the tracking and cancel calls. Shopify has no field for
it, so it is recovered from the tracking URL ClickPost writes onto the
fulfillment (see ``_extract_cp_id``). A waybill with no recoverable
``cp_id`` is refused rather than sent as a request the API would reject.

Status is read by polling ``track-order``. ClickPost also pushes status via
webhooks, which would avoid the polling entirely, but no webhook endpoint is
registered for this partner yet.

Stateless (per AGENTS.md): no state mutation, only reads via ``state`` for
client_id and trace correlation. All I/O is async via the shared httpx
client (``utils/http_client.py``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from fashion_bot.clickpost.tools.response_parser import (
    CANCELLED,
    DAMAGED,
    DELIVERED,
    DISPATCHED,
    IN_TRANSIT,
    LOST,
    ORDER_PLACED,
    OUT_FOR_DELIVERY,
    PICKED_UP,
    RETURN_TO_ORIGIN,
    parse_clickpost_tracking_response,
)
from fashion_bot.config_manager import aget_clickpost_config
from fashion_bot.core.logistics_router import PER_PARTNER_TIMEOUT_S
from fashion_bot.core.partner_response_mappings import (
    apply_order_data_mapping,
    coerce_etd_date,
)
from fashion_bot.core.vendor_config import resolve_partner_from_url
from fashion_bot.interfaces.logistics import LogisticsInterface
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)

# Required keys for a usable ClickPost config. Any missing/dummy key
# short-circuits every adapter method before a network call can fire.
# Auth is a static API key + username with no login step, and the hosts
# default internally (see _endpoints), so no base URL is required here.
_REQUIRED_CONFIG_KEYS = ("username", "key")

_DEFAULT_API_BASE = "https://www.clickpost.in"
# Tracking reads live on a separate host from the order/cancel APIs.
_DEFAULT_TRACKING_API_BASE = "https://api.clickpost.in"
# The predicted-SLA (expected date of delivery) model sits on a third host.
_DEFAULT_SLA_API_BASE = "https://ds.clickpost.in"

# Literal ClickPost-name match used for waybill resolution from Shopify
# fulfillments. Kept as a secondary signal only: in practice Shopify records
# the underlying courier here, not the aggregator (see below).
_CLICKPOST_NAME_MATCHES = frozenset({"clickpost", "click post"})
# Waybills the track-order API accepts in one comma-separated request.
_MAX_WAYBILLS_PER_CALL = 15
# Kept just below the router's fan-out deadline so a slow courier read fails
# as an HTTP timeout we can see, rather than being cancelled mid-flight by the
# router's wait_for with nothing to log.
_HTTP_TIMEOUT_S = max(1.0, PER_PARTNER_TIMEOUT_S - 1.0)
# ClickPost is an aggregator: Shopify records the *underlying* courier in
# tracking_company (Delhivery, Shadowfax, ...), never "clickpost", so the
# tracking URL host is the only reliable signal. Hosts are tenant-branded
# (``<tenant>.clickpost.ai``), so matching is domain-level and lives in
# ``vendor_config.TRACKING_URL_PATTERNS`` -- the one place that decides which
# platform a tracking URL belongs to.

# Statuses where a cancel attempt is a no-op (already done / terminal).
# Buckets 6 (Delivered), 7 (Returned) and 8 (Lost) are terminal in
# ClickPost's model; a damaged shipment is likewise past cancelling.
# Built from the parser's own vocabulary rather than string literals, so an
# entry cannot drift from the statuses the parser can actually emit.
# ("rto_delivered" previously sat here and matched nothing.)
_TERMINAL_STATUSES = frozenset({
    CANCELLED, DAMAGED, DELIVERED, LOST, RETURN_TO_ORIGIN,
})

# ClickPost's canonical statuses translated into the vocabulary the shared
# status resolver already speaks -- spaced and uppercase, as the other
# partners emit. Each partner translates on its way out rather than teaching
# the shared map its own dialect, so a new partner cannot change how an
# existing one's statuses resolve. Without this, ``in_transit`` reaches the
# resolver as IN_TRANSIT, matches nothing (upper() changes case, not
# separators), and the order_status prompt has no logistics_status to match
# its rules against.
#
# "dispatched" reads as in transit here, as it does in
# partner_response_mappings and in Delhivery's own adapter.
#
# Statuses with no counterpart in the shared vocabulary -- cancelled, lost,
# damaged, exception, return_to_origin -- are deliberately absent: the prompt
# has no rule for them, and mapping one to a near neighbour would describe a
# shipment as something it is not.
_SHARED_STATUS_VOCABULARY = {
    ORDER_PLACED: "NEW",
    DISPATCHED: "IN TRANSIT",
    PICKED_UP: "PICKED UP",
    IN_TRANSIT: "IN TRANSIT",
    OUT_FOR_DELIVERY: "OUT FOR DELIVERY",
    DELIVERED: "DELIVERED",
}


def _to_shared_status(canonical: Optional[str]) -> Optional[str]:
    """Translate a ClickPost status into the shared resolver's vocabulary.

    Returns the canonical value unchanged when there is no counterpart, so an
    unmapped status stays unresolved rather than being reported as a
    neighbouring state.
    """
    if not canonical:
        return canonical
    return _SHARED_STATUS_VOCABULARY.get(canonical, canonical)


def _humanize_status(canonical: Optional[str]) -> str:
    """Last-resort readable label from a canonical status.

    Only reached when the courier supplied neither a remark nor non-numeric
    status text; turns ``out_for_delivery`` into ``Out For Delivery`` so the
    customer still sees words.
    """
    return (canonical or "").replace("_", " ").strip().title()


def _extract_cp_id(tracking_url: Optional[str]) -> Optional[int]:
    """Pull ClickPost's courier-partner id out of a tracking URL.

    Both the tracking and cancel APIs require ``cp_id`` alongside the
    waybill -- it identifies which courier actually carries the shipment
    (Delhivery, Shadowfax, ...). Shopify does not store it as a field, but
    ClickPost embeds it in the tracking URL it writes onto the fulfillment
    (``https://<tenant>.clickpost.ai?cp_id=4&waybill=...``), which is the
    only place it is available. Returns ``None`` if absent/malformed.
    """
    if not tracking_url:
        return None
    try:
        raw = parse_qs(urlparse(tracking_url).query).get("cp_id", [None])[0]
        return int(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None


def _is_dummy_value(value: Optional[str]) -> bool:
    """True if ``value`` is empty or looks like a placeholder/test value."""
    if not value:
        return True
    n = value.strip().lower()
    if not n:
        return True
    return n.startswith("dummy") or n in {
        "placeholder", "test", "your_username", "your_api_key", "your_key",
    }


class ClickPostLogisticsAdapter(LogisticsInterface):
    """LogisticsInterface implementation for ClickPost. Every method checks
    its config first and returns ``configuration_missing`` rather than
    calling out, so a tenant without real, non-placeholder credentials stays
    inert; a configured tenant's calls reach live shipments."""

    def __init__(self, client_id: Optional[str] = None):
        self.client_id = client_id
        self._config: Optional[Dict[str, Any]] = None

    @classmethod
    async def create(cls, client_id: Optional[str] = None) -> "ClickPostLogisticsAdapter":
        """Async factory -- eagerly loads tenant config once."""
        adapter = cls(client_id=client_id)
        adapter._config = await aget_clickpost_config(client_id=client_id)
        return adapter

    # ── helpers ────────────────────────────────────────────────────────

    async def _aget_config(self, state: Optional[Dict] = None) -> Dict[str, Any]:
        if self._config is not None:
            return self._config
        cid = self.client_id or (state.get("client_id") if state else None)
        self._config = await aget_clickpost_config(client_id=cid) or {}
        return self._config

    @staticmethod
    def _raise_sync_unavailable(method_name: str):
        raise RuntimeError(
            f"ClickPostLogisticsAdapter.{method_name} is async-only. "
            f"Use the corresponding `await a...` method."
        )

    @staticmethod
    def _validate_config(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a ``configuration_missing`` result if any required key is
        absent or still a placeholder/dummy value; else ``None``."""
        config = config or {}
        for key in _REQUIRED_CONFIG_KEYS:
            if _is_dummy_value(config.get(key)):
                return {
                    "success": False,
                    "status": "configuration_missing",
                    "message": "ClickPost configuration missing or placeholder",
                }
        return None

    def _endpoints(self, config: Dict[str, Any]) -> Dict[str, str]:
        """The endpoints this adapter calls, in one place.

        Only the three that are actually invoked are listed; ClickPost's
        remaining APIs (order creation, courier recommendation, AWB
        registration, label fetch) belong to capabilities this adapter does
        not implement, and are left out rather than kept as dead entries.

        Three hosts are in play -- SLA predictions, tracking reads, and
        everything else each live on their own. ``track_order`` and
        ``cancel`` are GETs whose arguments all travel in the query string.
        """
        api_base = (config.get("api_base") or _DEFAULT_API_BASE).rstrip("/")
        tracking_base = (
            config.get("tracking_api_base") or _DEFAULT_TRACKING_API_BASE
        ).rstrip("/")
        sla_base = (config.get("sla_api_base") or _DEFAULT_SLA_API_BASE).rstrip("/")
        return {
            # Predicted delivery SLA between two pincodes, before any
            # shipment exists.
            "predicted_sla": f"{sla_base}/api/v2/predicted_sla_api/",
            # Reads a shipment's current status and scan history.
            "track_order": f"{tracking_base}/api/v2/track-order/",
            "cancel": f"{api_base}/api/v1/cancel-order/",
        }

    async def _resolve_waybill_from_shopify(
        self,
        order_ref: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Resolve ClickPost's waybill for an order from Shopify's fulfillment
        record. ClickPost is not Shopify-integrated yet, so the bot has no
        direct order surface on ClickPost's side -- the waybill has to come
        from whatever Shopify recorded for the fulfillment. Never raises."""
        try:
            from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter

            cid = self.client_id or (state.get("client_id") if state else None)
            shopify = ShopifyOrderAdapter(client_id=cid)
            order = await shopify.aget_order_details(order_ref, state=state)
            fulfillments = (order or {}).get("fulfillments") or []

            candidates: List[Dict[str, Any]] = []
            for fulfillment in fulfillments:
                if not isinstance(fulfillment, dict):
                    continue
                tracking_company = fulfillment.get("tracking_company") or ""
                tracking_url = fulfillment.get("tracking_url") or ""
                tracking_number = fulfillment.get("tracking_number")
                if not tracking_number:
                    numbers = fulfillment.get("tracking_numbers") or []
                    tracking_number = numbers[0] if numbers else None
                if not tracking_number:
                    continue

                # URL matching goes through the shared pattern table so there
                # is one definition of what a ClickPost tracking URL looks
                # like; the name check stays as a secondary signal.
                is_clickpost = (
                    tracking_company.strip().lower() in _CLICKPOST_NAME_MATCHES
                    or resolve_partner_from_url(tracking_url) == "clickpost"
                )
                if is_clickpost:
                    candidates.append({
                        "waybill": str(tracking_number),
                        "tracking_company": tracking_company,
                        "cp_id": _extract_cp_id(tracking_url),
                        "tracking_url": tracking_url,
                    })

            if not candidates:
                return {
                    "success": False,
                    "status": "waybill_missing",
                    "message": "ClickPost waybill not available in Shopify fulfillment",
                }

            # An order can ship as several parcels, each with its own waybill.
            # All of them are returned: a split order is not an unanswerable
            # question, it is two shipments the customer is waiting on, and
            # quoting only the first would present half the order as the whole.
            # The same waybill can appear on more than one fulfillment record,
            # so identical numbers collapse to one parcel.
            parcels: List[Dict[str, Any]] = []
            seen: set = set()
            for candidate in candidates:
                if candidate["waybill"] in seen:
                    continue
                seen.add(candidate["waybill"])
                parcels.append(candidate)

            primary = parcels[0]
            return {
                "success": True,
                # ``waybill`` stays the first parcel so single-shipment callers
                # read it unchanged; multi-parcel callers walk ``parcels``.
                "waybill": primary["waybill"],
                "cp_id": primary["cp_id"],
                "tracking_company": primary["tracking_company"],
                "tracking_url": primary["tracking_url"],
                "parcels": parcels,
                "source": "shopify_fulfillment",
            }
        except Exception as exc:
            log_with_trace_id(state, f"ClickPost waybill resolution failed: {exc}", "error")
            return {
                "success": False,
                "status": "waybill_missing",
                "message": "ClickPost waybill not available in Shopify fulfillment",
            }

    # ── mockable network helpers ────────────────────────────────────────
    # Real ClickPost tracking/cancel shapes are unverified pending live API
    # sample traffic -- these are the only methods that ever touch the
    # network, and each refuses to run against a missing/placeholder config
    # even when called directly (defense in depth on top of the public-method
    # config checks above).

    async def _track_waybill(
        self,
        waybill: str,
        cp_id: Optional[int] = None,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Read a shipment's status and scan history via the track-order API.

        A GET carrying ``username``, ``key``, ``waybill`` and the required
        ``cp_id`` in the query string. Distinct from ``awb-register``, which
        only subscribes an AWB to tracking and returns no status.
        """
        config = await self._aget_config(state)
        invalid = self._validate_config(config)
        if invalid:
            raise RuntimeError("ClickPost configuration missing or placeholder")
        if cp_id is None:
            raise RuntimeError(
                "ClickPost cp_id is required to track a waybill; it could not "
                "be resolved from the Shopify tracking URL."
            )

        params: Dict[str, Any] = {
            "username": config.get("username"),
            "key": config.get("key"),
            "waybill": waybill,
            "cp_id": cp_id,
        }

        endpoints = self._endpoints(config)
        client = await get_shared_async_http_client()
        t0 = time.monotonic()
        response = await client.get(endpoints["track_order"], params=params, timeout=_HTTP_TIMEOUT_S)
        log_with_trace_id(
            state,
            f"[CLICKPOST] GET track_order/{waybill} elapsed_ms={int((time.monotonic() - t0) * 1000)} "
            f"status={response.status_code}",
        )
        response.raise_for_status()
        return response.json()

    async def _cancel_waybill(
        self,
        waybill: str,
        cp_id: Optional[int] = None,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Call the Cancel API -- a GET carrying every argument as a URL
        parameter.

        A courier can refuse the cancellation while the request itself
        succeeds, so HTTP 200 alone does not mean the shipment was
        cancelled: the outcome lives in ``meta.success``, with the courier's
        reason in ``meta.message``. Both are checked before reporting
        success, otherwise a refused cancellation would be reported to the
        customer as done.
        """
        config = await self._aget_config(state)
        invalid = self._validate_config(config)
        if invalid:
            raise RuntimeError("ClickPost configuration missing or placeholder")
        if cp_id is None:
            raise RuntimeError(
                "ClickPost cp_id is required to cancel a waybill; it could not "
                "be resolved from the Shopify tracking URL."
            )

        params: Dict[str, Any] = {
            "username": config.get("username"),
            "key": config.get("key"),
            "waybill": waybill,
            "cp_id": cp_id,
        }
        account_code = config.get("account_code")
        if account_code:
            params["account_code"] = account_code

        endpoints = self._endpoints(config)
        client = await get_shared_async_http_client()
        t0 = time.monotonic()
        response = await client.get(endpoints["cancel"], params=params, timeout=_HTTP_TIMEOUT_S)
        log_with_trace_id(
            state,
            f"[CLICKPOST] GET cancel/{waybill} elapsed_ms={int((time.monotonic() - t0) * 1000)} "
            f"status={response.status_code}",
        )

        try:
            body = response.json()
        except ValueError:
            return {
                "success": False,
                "error": f"Cancel failed: {response.text or f'HTTP {response.status_code}'}",
            }

        meta = body.get("meta") if isinstance(body, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        if meta.get("success") is True:
            return {"success": True, "message": "Shipment cancelled in ClickPost"}
        return {
            "success": False,
            "error": f"Cancel failed: {meta.get('message') or f'HTTP {response.status_code}'}",
        }

    # ── interface methods ─────────────────────────────────────────────

    def get_tracking_details(self, tracking_number: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_tracking_details")

    async def aget_tracking_details(
        self,
        tracking_number: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ClickPostAdapter: tracking waybill {tracking_number}")
        config = await self._aget_config(state)
        invalid = self._validate_config(config)
        if invalid:
            return invalid

        # ClickPost identifies a shipment by (waybill, cp_id), but this
        # interface method receives only the waybill. ``state`` is the sole
        # place a caller can supply the courier id; without it the tracking
        # call cannot be built, so say so rather than sending a request that
        # is guaranteed to be rejected.
        cp_id = _extract_cp_id((state or {}).get("tracking_url")) or (state or {}).get("cp_id")
        if cp_id is None:
            return {
                "success": False,
                "status": "cp_id_missing",
                "message": "ClickPost requires a courier id (cp_id) alongside the waybill",
            }

        try:
            raw = await self._track_waybill(tracking_number, cp_id=cp_id, state=state)
            parsed = parse_clickpost_tracking_response(raw, waybill=tracking_number)
        except Exception as exc:
            log_with_trace_id(state, f"ClickPost tracking error: {exc}", "error")
            return {"success": False, "status": "error", "message": str(exc)}

        if not parsed.get("success"):
            return parsed

        # Emit the shared tracking shape. The router judges a tracking result
        # usable by awb/current_location/latest_activity, so returning the
        # parser's own key names would have every successful read discarded.
        location = parsed.get("location") or "N/A"
        raw_date = parsed.get("status_date") or "N/A"
        # ``latest_activity`` is shown to the customer, so it has to be the
        # courier's wording. Some couriers put a numeric code in ``status``
        # and the readable text in ``remark`` ("17" vs "Out For Delivery"),
        # so prefer the remark and reject a purely numeric status rather than
        # quoting a bare code back at them.
        _raw = (parsed.get("raw_status") or "").strip()
        activity = (
            (parsed.get("remark") or "").strip()
            or (_raw if not _raw.isdigit() else "")
            or _humanize_status(parsed.get("status"))
            or "N/A"
        )
        return {
            "success": True,
            "awb": parsed.get("waybill") or tracking_number,
            "status": parsed.get("status"),
            "current_location": location,
            "latest_activity": activity,
            "last_update_date": raw_date,
            "formatted_update": f"📍 Last update: {activity} at {location} on {raw_date}",
            "scans": parsed.get("scans") or [],
        }

    def create_shipment(self, order_details: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        # ClickPost documents a create-order API, but shipments are
        # manifested through the merchant's own ops flow rather than this
        # bot, so it is out of scope by decision -- not a capability gap.
        raise NotImplementedError(
            "ClickPostLogisticsAdapter does not create shipments. Manifesting "
            "is handled by the merchant's own ops flow, so this is out of "
            "scope by decision rather than an unfinished capability."
        )

    async def acreate_shipment(
        self,
        order_details: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError(
            "ClickPostLogisticsAdapter does not create shipments. Manifesting "
            "is handled by the merchant's own ops flow, so this is out of "
            "scope by decision rather than an unfinished capability."
        )

    def cancel_shipment(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("cancel_shipment")

    async def acancel_shipment(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ClickPostAdapter: cancelling shipment for order {order_id}")
        # Config first: it is a dict lookup, while resolving the waybill costs
        # a Shopify round-trip whose result an unconfigured tenant discards.
        config = await self._aget_config(state)
        invalid = self._validate_config(config)
        if invalid:
            return invalid

        resolved = await self._resolve_waybill_from_shopify(order_id, state=state)
        if not resolved.get("success"):
            return resolved

        # Reading every parcel is safe; cancelling one of several is not. A
        # split order would leave the rest of the shipment in flight while the
        # caller is told the order was cancelled, so this stays a refusal until
        # cancelling the whole order is specified.
        parcels = resolved.get("parcels") or []
        if len(parcels) > 1:
            return {
                "success": False,
                "status": "cancel_multi_parcel_unsupported",
                # The caller cannot recover from this on its own: Shopify may
                # already be cancelled by the time it is read. Flagged as manual
                # work so the cancellation flow escalates rather than logs.
                "requires_manual_action": True,
                "order_id": order_id,
                "waybills": [p["waybill"] for p in parcels],
                "message": (
                    "Order ships as multiple ClickPost parcels; cancelling a "
                    "single waybill would leave the others in transit."
                ),
            }

        waybill = resolved["waybill"]
        cp_id = resolved.get("cp_id")
        try:
            tracking_raw = await self._track_waybill(waybill, cp_id=cp_id, state=state)
            current = parse_clickpost_tracking_response(tracking_raw, waybill=waybill)
        except Exception as exc:
            log_with_trace_id(state, f"ClickPost status lookup before cancel failed: {exc}", "error")
            return {"success": False, "order_id": order_id, "waybill": waybill, "error": str(exc)}

        current_status = current.get("status") if current.get("success") else None

        if current_status in _TERMINAL_STATUSES:
            return {
                "success": False,
                "skipped": True,
                "order_id": order_id,
                "waybill": waybill,
                "status": current_status,
                "message": (
                    f"ClickPost shipment already in terminal status "
                    f"'{current_status}'; not cancelling."
                ),
            }

        if not current_status or current_status == "unknown":
            # Conservative: an unrecognised/unavailable status must not be
            # treated as "safe to cancel".
            return {
                "success": False,
                "skipped": True,
                "order_id": order_id,
                "waybill": waybill,
                "status": current_status,
                "message": "ClickPost current status unknown; refusing to cancel (conservative).",
            }

        try:
            result = await self._cancel_waybill(waybill, cp_id=cp_id, state=state)
        except Exception as exc:
            log_with_trace_id(state, f"ClickPost cancel error: {exc}", "error")
            return {"success": False, "order_id": order_id, "waybill": waybill, "error": str(exc)}

        if not isinstance(result, dict):
            result = {"success": False, "error": "Invalid response from ClickPost cancel call"}
        return {"order_id": order_id, "waybill": waybill, **result}

    def get_order_data(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_order_data")

    async def aget_order_data(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        # Config first: it is a dict lookup, while resolving the waybill costs
        # a Shopify round-trip whose result an unconfigured tenant discards.
        config = await self._aget_config(state)
        invalid = self._validate_config(config)
        if invalid:
            return invalid

        resolved = await self._resolve_waybill_from_shopify(order_id, state=state)
        if not resolved.get("success"):
            return resolved

        parcels = resolved.get("parcels") or [{
            "waybill": resolved["waybill"],
            "cp_id": resolved.get("cp_id"),
            "tracking_company": resolved.get("tracking_company"),
            "tracking_url": resolved.get("tracking_url"),
        }]
        try:
            tracked = await self._track_parcels(parcels, state=state)
        except Exception as exc:
            log_with_trace_id(state, f"ClickPost aget_order_data error: {exc}", "error")
            return {"success": False, "found": False, "error": str(exc)}

        # A parcel the courier does not recognise is a clean miss, not a
        # failure: the router distinguishes the two, and reporting it as an
        # error makes a routine response look like a partner outage. On a
        # split order the parcels that did resolve are still worth reporting,
        # so only a total miss returns the miss.
        usable = [(p, d) for p, d in tracked if d.get("success") and d.get("found") is not False]
        if not usable:
            if not tracked:
                # Every chunk raised -- gather swallowed them so the parcels
                # that did resolve could still be reported. With none left,
                # say so: the router stores this dict in ``per_partner``, and
                # without the key a total outage reads there like a
                # well-formed empty answer.
                return {
                    "success": False,
                    "found": False,
                    "error": "ClickPost tracking failed for every parcel",
                }
            first = tracked[0][1]
            if first.get("found") is False:
                return {"success": True, "found": False, **first}
            return {"success": False, "found": False, **first}

        shipment_views = [(parcel, self._build_shipment(parcel, parsed))
                          for parcel, parsed in usable]

        # The order is only complete once its last parcel lands, so the
        # order-level view follows the parcel the customer is still waiting
        # on. Dates are parsed before comparing: a split order is exactly the
        # case where two couriers supply the ETD, and comparing their free-text
        # dates as strings would rank "27-06-2026" above "2026-07-01" and
        # promise the whole order early. An unparseable date sorts oldest so it
        # can never win the comparison.
        in_flight = [
            pair for pair in shipment_views if pair[1].get("status") != DELIVERED
        ]
        if in_flight:
            # Still moving: the latest estimate is what the customer waits for.
            primary_parcel, primary_shipments = max(
                in_flight,
                key=lambda pair: coerce_etd_date(pair[1].get("etd")) or date.min,
            )
        else:
            # All arrived: select on when they actually did, not on estimates
            # couriers commonly stop updating once a parcel is delivered --
            # keying on ETD here would leave ``delivered_on`` reporting the
            # earliest parcel's date as the whole order's.
            primary_parcel, primary_shipments = max(
                shipment_views,
                key=lambda pair: coerce_etd_date(pair[1].get("delivered_date")) or date.min,
            )
        primary_parsed = dict(primary_shipments.get("_parsed") or {})

        shipments = dict(primary_shipments)
        shipments.pop("_parsed", None)
        if len(shipment_views) > 1:
            # Per-parcel detail so callers can answer for each shipment rather
            # than collapsing a split order into one date.
            shipments["parcels"] = [
                {k: v for k, v in view.items() if k != "_parsed"}
                for _, view in shipment_views
            ]
        order_data = {**primary_parsed, "shipments": shipments}
        return {
            "success": True,
            "found": True,
            "order_id": order_id,
            "logistics_order_id": primary_parsed.get("waybill"),
            # Translated on the way out: this is the field the shared status
            # resolver reads. ``shipments``/``order_data`` keep the canonical
            # lower_snake form the parser, the terminal-status set and the
            # partner mappings all work in.
            "status": _to_shared_status(
                shipments.get("status") or primary_parsed.get("status")
            ),
            "order_data": order_data,
            "shipments": shipments,
            "delivered_on": shipments.get("delivered_date"),
        }

    def _build_shipment(
        self,
        parcel: Dict[str, Any],
        parsed: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Shape one parcel's tracking into the canonical shipment dict.

        Canonical field names come from partner_response_mappings so they stay
        declared in one place (``etd`` in particular is what
        extract_expected_delivery reads). Courier and tracking URL are not in
        the tracking response at all -- they come from the Shopify fulfillment
        -- so they are merged in after the mapping.
        """
        mapped = apply_order_data_mapping("clickpost", {
            **parsed,
            "delivered_date": (
                parsed.get("status_date") if parsed.get("status") == "delivered" else None
            ),
        })
        shipments = mapped.get("shipments") or {}
        shipments.setdefault("scans", parsed.get("scans") or [])
        shipments["courier"] = parcel.get("tracking_company")
        shipments["tracking_url"] = parcel.get("tracking_url")
        shipments.setdefault("awb", parsed.get("waybill") or parcel.get("waybill"))
        shipments.setdefault("status", parsed.get("status"))
        # Carried so the order-level view keeps the parser's own fields for the
        # parcel it selects; stripped before the shipment dict reaches callers.
        shipments["_parsed"] = {**parsed, **mapped}
        return shipments

    async def _track_parcels(
        self,
        parcels: List[Dict[str, Any]],
        state: Optional[Dict] = None,
    ) -> List[tuple]:
        """Track every parcel of an order, pairing each with its parsed result.

        The track-order API takes up to 15 comma-separated waybills per call
        and keys the response by waybill, so parcels sharing a ``cp_id`` are
        read in one request rather than one request each. A split across two
        couriers needs one call per ``cp_id`` -- it is a single query
        parameter.
        """
        by_cp_id: Dict[Any, List[Dict[str, Any]]] = {}
        for parcel in parcels:
            by_cp_id.setdefault(parcel.get("cp_id"), []).append(parcel)

        chunks = [
            (cp_id, group[start:start + _MAX_WAYBILLS_PER_CALL])
            for cp_id, group in by_cp_id.items()
            for start in range(0, len(group), _MAX_WAYBILLS_PER_CALL)
        ]

        # Independent reads: one courier's response never gates another's, and
        # a single order can span several cp_ids. return_exceptions keeps one
        # courier's outage from sinking the parcels that did resolve -- the
        # caller above already treats a partial read as a partial answer.
        raws = await asyncio.gather(
            *(
                self._track_waybill(
                    ",".join(p["waybill"] for p in chunk), cp_id=cp_id, state=state,
                )
                for cp_id, chunk in chunks
            ),
            return_exceptions=True,
        )

        tracked: List[tuple] = []
        for (_cp_id, chunk), raw in zip(chunks, raws):
            if isinstance(raw, BaseException):
                log_with_trace_id(
                    state, f"ClickPost track chunk failed: {raw}", "warning",
                )
                continue
            for parcel in chunk:
                tracked.append((
                    parcel,
                    parse_clickpost_tracking_response(
                        raw,
                        waybill=parcel["waybill"],
                        # A single-waybill request can tolerate a key echoed in
                        # a different form. A batched one cannot: the other
                        # record in the response belongs to a different parcel,
                        # and reporting it here would give one parcel another's
                        # status.
                        allow_single_fallback=len(chunk) == 1,
                    ),
                ))
        return tracked

    async def aget_matching_orders(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """Return this order in the list wrapper the order-status path expects.

        ``OrderStatusOrchestrator`` races the *order* adapters, not the
        logistics ones, so without this the tracked status never reaches the
        agent and every fulfilled order falls back to the Shopify-derived
        state. Mirrors the shape the other partners' adapters return so
        processors stay vendor-neutral.
        """
        result = await self.aget_order_data(order_id, state=state)
        if not result.get("found"):
            return []
        return [{
            "order_id": result.get("logistics_order_id"),   # waybill
            "channel_order_id": order_id,
            # ``status`` is the partner-reported one, in the shared spaced
            # vocabulary the resolver and the fulfillment mapping both read.
            "status": result.get("status", ""),
            # ``shipment_status`` mirrors Shopify's carrier field, which is
            # lower_snake everywhere else in the codebase -- the resolver's
            # own map is keyed that way -- so it keeps the parser's form.
            "shipment_status": (result.get("shipments") or {}).get("status", ""),
            "order_data": result.get("order_data", {}),
        }]

    # ── delivery estimate ───────────────────────────────────────────────

    async def aget_delivery_estimate(
        self,
        pickup_pincode: str,
        destination_pincode: str,
        weight: float = 0.5,
        cod: bool = False,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Predicted delivery SLA between two pincodes.

        Calls ClickPost's predicted-SLA model, which returns a min/max range
        in days plus the courier expected to be fastest. ``weight`` and
        ``cod`` are part of the shared interface but the model does not
        accept them, so they are ignored rather than silently affecting the
        answer.

        The payload is a *list* of pincode pairs -- one entry is sent here,
        and a list-shaped ``result`` is unwrapped accordingly.
        """
        config = await self._aget_config(state)
        invalid = self._validate_config(config)
        if invalid:
            return {"status": "error", "message": invalid["message"]}

        try:
            pickup = int(str(pickup_pincode).strip())
            drop = int(str(destination_pincode).strip())
        except (TypeError, ValueError):
            return {
                "status": "error",
                "message": "Pincodes must be numeric for a ClickPost delivery estimate",
            }

        endpoints = self._endpoints(config)
        try:
            client = await get_shared_async_http_client()
            t0 = time.monotonic()
            response = await client.post(
                endpoints["predicted_sla"],
                params={"username": config.get("username"), "key": config.get("key")},
                json=[{"pickup_pincode": pickup, "drop_pincode": drop}],
                headers={"Content-type": "application/json"},
                timeout=_HTTP_TIMEOUT_S,
            )
            log_with_trace_id(
                state,
                f"[CLICKPOST] POST predicted_sla {pickup}->{drop} "
                f"elapsed_ms={int((time.monotonic() - t0) * 1000)} "
                f"status={response.status_code}",
            )
            body = response.json()
        except Exception as exc:
            log_with_trace_id(state, f"ClickPost delivery estimate error: {exc}", "error")
            return {"status": "error", "message": str(exc)}

        meta = body.get("meta") if isinstance(body, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        if meta.get("success") is not True:
            return {
                "status": "error",
                "message": meta.get("message") or "ClickPost could not predict an SLA",
            }

        result = body.get("result")
        if isinstance(result, list):
            result = result[0] if result else {}
        if not isinstance(result, dict):
            return {"status": "error", "message": "Malformed ClickPost SLA response"}

        sla_min = result.get("predicted_sla_min")
        sla_max = result.get("predicted_sla_max")
        if sla_min is None and sla_max is None:
            return {
                "status": "error",
                "message": "No SLA predicted between these pincodes",
            }

        all_map = result.get("all_map")
        if sla_min is not None and sla_max is not None and sla_min != sla_max:
            estimated = f"{sla_min}-{sla_max} days"
        else:
            only = sla_min if sla_min is not None else sla_max
            estimated = f"{only} days"

        return {
            "status": "success",
            "origin_pincode": pickup_pincode,
            "destination_pincode": destination_pincode,
            # ClickPost names the fastest courier by numeric id, and resolving
            # that to a label needs their courier-partner list. Callers read
            # ``best_courier`` as a human-readable courier name, so reporting
            # the id there would surface a bare number; it is left unset and
            # published under its own key instead.
            "best_courier": None,
            "min_sla_cp_id": result.get("min_sla_cp_id"),
            "estimated_delivery": estimated,
            "predicted_sla_min": sla_min,
            "predicted_sla_max": sla_max,
            "all_options_count": len(all_map) if isinstance(all_map, dict) else 0,
        }

    # ── not supported yet (safe stubs, no I/O) ──────────────────────────
    # ClickPost publishes no endpoint for editing a manifested shipment, so
    # these stay stubs. Tenants configured for cancel-and-recreate never
    # reach them: that flow rewrites the order in the order vendor instead.

    async def aupdate_shipment_address(
        self,
        order_id: str,
        address_data: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        return {"success": False, "message": "Not supported for ClickPost yet"}

    async def aupdate_shipment_phone(
        self,
        order_id: str,
        new_phone: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        return {"success": False, "message": "Not supported for ClickPost yet"}

    async def aupdate_shipment_email(
        self,
        order_id: str,
        new_email: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        return {"success": False, "message": "Not supported for ClickPost yet"}

    async def aupdate_shipment_name(
        self,
        order_id: str,
        first_name: str,
        last_name: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        return {"success": False, "message": "Not supported for ClickPost yet"}
