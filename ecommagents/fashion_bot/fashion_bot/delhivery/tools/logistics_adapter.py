"""
Delhivery logistics adapter — implements LogisticsInterface for the
Delhivery (https://www.delhivery.com/) delivery partner.

Auth: static Bearer-style `Authorization: Token <api_token>` header, where
`api_token` comes from `client_configs.delhivery_details` (per-tenant). No
login flow; the token is long-lived and rotated by Delhivery's BD SPOC.

Endpoints (default `api_base = https://track.delhivery.com`; override via
`delhivery_details.api_base`):
  - GET  /api/v1/packages/json/?ref_ids={order_id}     # find order by ref id
  - GET  /api/v1/packages/json/?waybill={awb}          # tracking by AWB
  - POST /api/p/edit                                   # cancel + edit
  - GET  /c/api/pin-codes/json/?filter_codes={pin}     # serviceability
  - POST /api/cmu/create.json (form: format=json&data=)# adhoc create

Stateless (per AGENTS.md): no state mutation, only reads via `state` for
client_id and trace correlation.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from fashion_bot.config_manager import aget_delhivery_config
from fashion_bot.core.partner_response_mappings import (
    apply_order_data_mapping,
    extract_raw_payload,
)
from fashion_bot.interfaces.logistics import LogisticsInterface
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)

# Delhivery package statuses where we can still mutate the package.
_MUTABLE_STATUSES = frozenset({
    "MANIFESTED", "PENDING", "OPEN", "SCHEDULED", "IN TRANSIT",
})
# Statuses where mutations are silently no-op'd (already terminal/late).
_TERMINAL_STATUSES = frozenset({
    "DELIVERED", "CANCELLED", "CANCELED", "RTO", "RTO DELIVERED",
    "LOST", "DAMAGED",
})


class DelhiveryLogisticsAdapter(LogisticsInterface):
    """LogisticsInterface implementation for Delhivery."""

    def __init__(self, client_id: Optional[str] = None):
        self.client_id = client_id
        self._config: Optional[Dict[str, Any]] = None

    @classmethod
    async def create(cls, client_id: Optional[str] = None) -> "DelhiveryLogisticsAdapter":
        """Async factory — eagerly loads tenant config once."""
        adapter = cls(client_id=client_id)
        adapter._config = await aget_delhivery_config(client_id=client_id)
        return adapter

    # ── helpers ────────────────────────────────────────────────────────

    async def _aget_config(self, state: Optional[Dict] = None) -> Dict[str, Any]:
        if self._config is not None:
            return self._config
        cid = self.client_id or (state.get("client_id") if state else None)
        self._config = await aget_delhivery_config(client_id=cid) or {}
        return self._config

    @staticmethod
    def _auth_headers(token: str) -> Dict[str, str]:
        return {"Authorization": f"Token {token}", "Accept": "application/json"}

    @staticmethod
    def _clean_phone(phone: Optional[str]) -> str:
        digits = re.sub(r"\D", "", str(phone or ""))
        if digits.startswith("91") and len(digits) > 10:
            digits = digits[2:]
        if digits.startswith("0") and len(digits) > 10:
            digits = digits[1:]
        return digits[-10:] if len(digits) >= 10 else digits

    @staticmethod
    def _raise_sync_unavailable(method_name: str):
        raise RuntimeError(
            f"DelhiveryLogisticsAdapter.{method_name} is async-only. "
            f"Use the corresponding `await a...` method."
        )

    def _warn_missing_token(
        self,
        method_name: str,
        identifier: Optional[str] = None,
        state: Optional[Dict] = None,
    ) -> None:
        """Log a clear warning when an adapter method exits early because
        ``api_token`` is missing/empty in ``client_configs.delhivery_details``.

        Without this, the early-return path is completely silent — the
        adapter is invoked but emits no log line, so a tenant whose
        Delhivery config drifts (token rotated dashboard-side but DB not
        updated) looks indistinguishable from "Delhivery skipped me on
        purpose" in traces. We learned this the hard way during the
        Concept Groove rollout.
        """
        cid = self.client_id or (state.get("client_id") if state else None)
        suffix = f" identifier={identifier}" if identifier else ""
        logger.warning(
            f"[DELHIVERY] {method_name} skipped: missing api_token for "
            f"client_id={cid}{suffix} — check "
            f"client_configs.delhivery_details and restart the app to "
            f"clear cached config."
        )

    @staticmethod
    def _normalize_status(raw: Optional[str]) -> str:
        """Map Delhivery scan status text to canonical upper-case statuses
        compatible with classify_status / orchestrator status checks."""
        s = (raw or "").upper().strip()
        if not s:
            return ""
        if "DELIVERED" in s and "RTO" in s:
            return "RTO DELIVERED"
        if s == "DELIVERED":
            return "DELIVERED"
        if "RTO" in s:
            return "RTO"
        if "CANCEL" in s:
            return "CANCELED"
        if "OUT FOR DELIVERY" in s:
            return "OUT FOR DELIVERY"
        if "IN TRANSIT" in s or "DISPATCHED" in s:
            return "IN TRANSIT"
        if s in {"MANIFESTED", "PENDING", "OPEN", "SCHEDULED"}:
            return s
        return s

    @staticmethod
    def _extract_shipment(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Pull the first ``Shipment`` block out of Delhivery's nested payload.

        Delegates to the shared mapping module so this extraction stays in
        sync with the field-map below — both come from the same module.
        """
        return extract_raw_payload("delhivery", "get_order_data", payload)

    def _shipment_to_order_data(self, shipment: Dict[str, Any]) -> Dict[str, Any]:
        """Translate a Delhivery ``Shipment`` block into the canonical order_data dict.

        The actual mapping lives in ``core.partner_response_mappings``
        (``GET_ORDER_DATA_FIELD_MAPS["delhivery"]``) so adding a new partner
        does not require editing this adapter.
        """
        return apply_order_data_mapping("delhivery", shipment)

    # ── interface methods ─────────────────────────────────────────────

    def get_tracking_details(self, tracking_number: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_tracking_details")

    async def aget_tracking_details(
        self,
        tracking_number: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"DelhiveryAdapter: tracking AWB {tracking_number}")
        config = await self._aget_config(state)
        token = config.get("api_token")
        api_base = config.get("api_base") or "https://track.delhivery.com"
        if not token:
            self._warn_missing_token("aget_tracking_details", tracking_number, state)
            return {"status": "error", "message": "Delhivery configuration missing"}

        client = await get_shared_async_http_client()
        try:
            t0 = time.monotonic()
            response = await client.get(
                f"{api_base}/api/v1/packages/json/",
                params={"waybill": tracking_number},
                headers=self._auth_headers(token),
                timeout=30,
            )
            logger.info(
                f"[DELHIVERY] GET track/awb/{tracking_number} "
                f"elapsed_ms={int((time.monotonic() - t0) * 1000)} status={response.status_code}"
            )
            if response.status_code != 200:
                return {"status": "error", "message": f"API Error: {response.status_code}"}
            payload = response.json()
            shipment = self._extract_shipment(payload)
            if not shipment:
                return {
                    "awb": tracking_number,
                    "status": "No tracking updates logged yet",
                    "current_location": "N/A",
                    "latest_activity": "N/A",
                    "last_update_date": "N/A",
                }
            status_block = shipment.get("Status") or {}
            return {
                "awb": tracking_number,
                "status": "Tracking available",
                "current_location": status_block.get("StatusLocation", ""),
                "latest_activity": (status_block.get("Status") or "").upper(),
                "last_update_date": status_block.get("StatusDateTime", ""),
                "formatted_update": (
                    f"📍 Last update: {status_block.get('Status', '')} at "
                    f"{status_block.get('StatusLocation', '')} on "
                    f"{status_block.get('StatusDateTime', '')}"
                ),
            }
        except httpx.HTTPError as exc:
            log_with_trace_id(state, f"Delhivery tracking error: {exc}", "error")
            return {"status": "error", "message": str(exc)}

    def create_shipment(self, order_details: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        # Order creation is Shopify's job — Shopify's Delhivery integration
        # auto-syncs new/edited orders to Delhivery, where a fresh AWB is
        # generated. We never create on Delhivery directly.
        raise NotImplementedError(
            "DelhiveryLogisticsAdapter does not create shipments. "
            "Orders are created on Shopify and auto-synced to Delhivery."
        )

    async def acreate_shipment(
        self,
        order_details: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        # See sync version above for rationale.
        raise NotImplementedError(
            "DelhiveryLogisticsAdapter does not create shipments. "
            "Orders are created on Shopify and auto-synced to Delhivery."
        )

    def cancel_shipment(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("cancel_shipment")

    async def acancel_shipment(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"DelhiveryAdapter: cancelling shipment for order {order_id}")
        config = await self._aget_config(state)
        token = config.get("api_token")
        api_base = config.get("api_base") or "https://track.delhivery.com"
        if not token:
            self._warn_missing_token("acancel_shipment", order_id, state)
            return {"success": False, "error": "Delhivery configuration missing", "order_id": order_id}

        # Need the AWB to cancel — look it up first.
        order_data_result = await self.aget_order_data(order_id, state=state)
        if not order_data_result.get("found"):
            return {"success": False, "error": "Order not found in Delhivery", "order_id": order_id}
        awb = (order_data_result.get("order_data") or {}).get("shipments", {}).get("awb")
        if not awb:
            return {"success": False, "error": "AWB not found for order", "order_id": order_id}

        # Status guard.
        current_status = (order_data_result.get("status") or "").upper()
        if current_status in _TERMINAL_STATUSES:
            return {
                "success": False,
                "skipped": True,
                "order_id": order_id,
                "error": f"Order already in terminal status '{current_status}', cannot cancel.",
            }

        client = await get_shared_async_http_client()
        try:
            response = await client.post(
                f"{api_base}/api/p/edit",
                json={"waybill": awb, "cancellation": "true"},
                headers={**self._auth_headers(token), "Content-Type": "application/json"},
                timeout=30,
            )
            if response.status_code in [200, 201, 202]:
                log_with_trace_id(state, f"Delhivery shipment cancelled for order {order_id} (AWB {awb})")
                return {
                    "success": True,
                    "order_id": order_id,
                    "awb": awb,
                    "message": "Shipment cancelled in Delhivery",
                }
            return {
                "success": False,
                "order_id": order_id,
                "error": f"Cancel failed: {response.text or f'HTTP {response.status_code}'}",
            }
        except httpx.HTTPError as exc:
            log_with_trace_id(state, f"Delhivery cancel error: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def get_delivery_estimate(
        self,
        pickup_pincode: str,
        destination_pincode: str,
        weight: float = 0.5,
        cod: bool = False,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_delivery_estimate")

    async def aget_delivery_estimate(
        self,
        pickup_pincode: str,
        destination_pincode: str,
        weight: float = 0.5,
        cod: bool = False,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(
            state,
            f"DelhiveryAdapter: ETA from {pickup_pincode} to {destination_pincode}",
        )
        config = await self._aget_config(state)
        token = config.get("api_token")
        api_base = config.get("api_base") or "https://track.delhivery.com"
        if not token:
            self._warn_missing_token(
                "aget_delivery_estimate",
                f"{pickup_pincode}→{destination_pincode}",
                state,
            )
            return {"status": "error", "message": "Delhivery configuration missing"}

        client = await get_shared_async_http_client()
        try:
            response = await client.get(
                f"{api_base}/c/api/pin-codes/json/",
                params={"filter_codes": str(destination_pincode).strip()},
                headers=self._auth_headers(token),
                timeout=30,
            )
            if response.status_code != 200:
                return {"status": "error", "message": f"API Error: {response.status_code}"}

            data = response.json()
            entries = data.get("delivery_codes") or []
            if not entries:
                return {"status": "error", "message": "Pincode not serviceable by Delhivery"}

            entry = (entries[0] or {}).get("postal_code") or {}
            cod_supported = (entry.get("cod") or "").upper() == "Y"
            prepaid_supported = (entry.get("pre_paid") or "").upper() == "Y"

            if cod and not cod_supported:
                return {
                    "status": "error",
                    "message": "Pincode is not COD-serviceable by Delhivery",
                }
            if not cod and not prepaid_supported:
                return {
                    "status": "error",
                    "message": "Pincode is not prepaid-serviceable by Delhivery",
                }

            return {
                "status": "success",
                "origin_pincode": pickup_pincode,
                "destination_pincode": destination_pincode,
                "best_courier": "Delhivery",
                "estimated_delivery": "3-5 days",
                "all_options_count": 1,
                "cod_available": cod_supported,
                "prepaid_available": prepaid_supported,
                "raw": entry,
            }
        except Exception as exc:
            log_with_trace_id(state, f"Delhivery ETA error: {exc}", "error")
            return {"status": "error", "message": str(exc)}

    def update_shipment_address(self, order_id: str, address_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("update_shipment_address")

    async def aupdate_shipment_address(
        self,
        order_id: str,
        address_data: Dict[str, Any],
        state: Optional[Dict] = None,
        skip_status_check: bool = False,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"DelhiveryAdapter: updating address for order {order_id}")
        config = await self._aget_config(state)
        token = config.get("api_token")
        api_base = config.get("api_base") or "https://track.delhivery.com"
        if not token:
            self._warn_missing_token("aupdate_shipment_address", order_id, state)
            return {"success": False, "error": "Delhivery configuration missing", "order_id": order_id}

        # Look up the AWB and current status.
        order_data_result = await self.aget_order_data(order_id, state=state)
        if not order_data_result.get("found"):
            return {"success": False, "error": "Order not found in Delhivery", "order_id": order_id}
        awb = (order_data_result.get("order_data") or {}).get("shipments", {}).get("awb")
        if not awb:
            return {"success": False, "error": "AWB not found for order", "order_id": order_id}

        current_status = (order_data_result.get("status") or "").upper()
        if not skip_status_check and current_status not in _MUTABLE_STATUSES:
            return {
                "success": False,
                "skipped": True,
                "order_id": order_id,
                "error": (
                    f"Delhivery does not allow address edits in status '{current_status}'."
                ),
            }

        # Map our generic address_data to Delhivery's edit fields.
        edit_payload: Dict[str, Any] = {"waybill": awb}
        if address_data.get("name"):
            edit_payload["name"] = address_data["name"]
        if address_data.get("phone"):
            edit_payload["phone"] = self._clean_phone(address_data["phone"])
        if address_data.get("address1") or address_data.get("address"):
            edit_payload["add"] = address_data.get("address1") or address_data.get("address")
        if address_data.get("address2"):
            edit_payload["add2"] = address_data["address2"]
        if address_data.get("city"):
            edit_payload["city"] = address_data["city"]
        if address_data.get("state"):
            edit_payload["state"] = address_data["state"]
        if address_data.get("zip"):
            edit_payload["pin"] = str(address_data["zip"])
        if address_data.get("country"):
            edit_payload["country"] = address_data["country"]
        if address_data.get("email"):
            edit_payload["email"] = address_data["email"]

        if len(edit_payload) <= 1:  # only waybill
            return {
                "success": True,
                "order_id": order_id,
                "skipped": True,
                "message": "No editable fields supplied for Delhivery edit.",
            }

        client = await get_shared_async_http_client()
        try:
            response = await client.post(
                f"{api_base}/api/p/edit",
                json=edit_payload,
                headers={**self._auth_headers(token), "Content-Type": "application/json"},
                timeout=30,
            )
            if response.status_code in [200, 201, 202]:
                return {
                    "success": True,
                    "order_id": order_id,
                    "awb": awb,
                    "message": "Address updated in Delhivery",
                }
            return {
                "success": False,
                "order_id": order_id,
                "error": f"Update failed: {response.text or f'HTTP {response.status_code}'}",
            }
        except httpx.HTTPError as exc:
            log_with_trace_id(state, f"Delhivery address update error: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def update_shipment_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("update_shipment_phone")

    async def aupdate_shipment_phone(
        self,
        order_id: str,
        new_phone: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        return await self.aupdate_shipment_address(
            order_id, {"phone": new_phone}, state=state,
        )

    def update_shipment_email(self, order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("update_shipment_email")

    async def aupdate_shipment_email(
        self,
        order_id: str,
        new_email: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        return await self.aupdate_shipment_address(
            order_id, {"email": new_email}, state=state, skip_status_check=True,
        )

    def update_shipment_name(self, order_id: str, first_name: str, last_name: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("update_shipment_name")

    async def aupdate_shipment_name(
        self,
        order_id: str,
        first_name: str,
        last_name: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        full_name = f"{first_name} {last_name}".strip()
        return await self.aupdate_shipment_address(
            order_id, {"name": full_name}, state=state,
        )

    def get_order_data(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_order_data")

    async def aget_order_data(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Fetch Delhivery's view of an order by its `ref_ids` (channel order id)."""
        config = await self._aget_config(state)
        token = config.get("api_token")
        api_base = config.get("api_base") or "https://track.delhivery.com"
        if not token:
            self._warn_missing_token("aget_order_data", order_id, state)
            return {"success": False, "found": False, "error": "Delhivery configuration missing"}

        client = await get_shared_async_http_client()
        try:
            t0 = time.monotonic()
            # Delhivery's tracking API matches `ref_ids` against the
            # tenant's stored channel order_number **verbatim**, which
            # depends on how their Delhivery Shopify connector is
            # configured:
            #
            #   - Shopify-default tenants (e.g. Concept Groove) store
            #     ``name`` with a leading ``#`` → ``"#gv15361"``.
            #   - Tenants whose connector uses ``order_number`` store
            #     the bare numeric → ``"15361"``.
            #   - Tenants with a non-standard Shopify ``order_prefix``
            #     store ``name`` with that prefix → ``"BLOOM-gv15361"``.
            #
            # So the prefix is sourced from
            # ``client_configs.delhivery_details.order_id_prefix``
            # (default ``"#"``). The adapter strips any leading ``"#"``
            # from the input ``order_id`` so callers can pass either
            # ``gv15361`` or ``#gv15361`` and we always emit exactly the
            # tenant's configured form.
            #
            # Diagnosed for Concept Groove via direct curl against
            # gv15361 (AWB 55434610000011, "Ready For Pickup"):
            #   ?ref_ids=gv15361    → empty   (no prefix)
            #   ?ref_ids=%23gv15361 → populated (with "#")
            # Case-sensitive: %23GV15361 also empty.
            prefix = config.get("order_id_prefix")
            if prefix is None:
                prefix = "#"
            normalized = str(order_id).lstrip("#")
            ref_ids_param = f"{prefix}{normalized}"
            response = await client.get(
                f"{api_base}/api/v1/packages/json/",
                params={"ref_ids": ref_ids_param},
                headers=self._auth_headers(token),
                timeout=30,
            )
            logger.info(
                f"[DELHIVERY] GET ref_ids/{ref_ids_param} "
                f"elapsed_ms={int((time.monotonic() - t0) * 1000)} status={response.status_code}"
            )
            if response.status_code != 200:
                return {
                    "success": False,
                    "found": False,
                    "error": f"API Error: {response.status_code}",
                }

            payload = response.json()
            shipment = self._extract_shipment(payload)
            if not shipment:
                return {
                    "success": False,
                    "found": False,
                    "message": f"Order {order_id} not found in Delhivery",
                }

            order_data = self._shipment_to_order_data(shipment)
            status = self._normalize_status((shipment.get("Status") or {}).get("Status"))
            return {
                "success": True,
                "found": True,
                "order_id": order_id,
                "logistics_order_id": shipment.get("AWB"),
                "status": status,
                "order_data": order_data,
                "shipments": order_data.get("shipments", {}),
                "delivered_on": order_data.get("delivered_date"),
            }
        except Exception as exc:
            log_with_trace_id(state, f"Delhivery aget_order_data error: {exc}", "error")
            return {"success": False, "found": False, "error": str(exc)}

    # ── helper used by DelhiveryOrderAdapter ─────────────────────────

    async def aget_matching_orders(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """Return Delhivery orders matching a given channel order id, in the
        same wrapper shape Shiprocket's adapter uses (a list of
        ``{"order_id", "channel_order_id", "status", "order_data"}`` dicts)
        so processors and orchestrator code can be vendor-neutral."""
        result = await self.aget_order_data(order_id, state=state)
        if not result.get("found"):
            return []
        return [{
            "order_id": result.get("logistics_order_id"),  # AWB
            "channel_order_id": order_id,
            "status": result.get("status", ""),
            "shipment_status": result.get("status", ""),
            "order_data": result.get("order_data", {}),
        }]
