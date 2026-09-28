import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Dict, Any, Optional
import httpx
from fashion_bot.config_manager import aget_config, aget_shiprocket_config
from fashion_bot.core.partner_response_mappings import apply_order_data_mapping
from fashion_bot.interfaces.logistics import LogisticsInterface
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.utils import log_with_trace_id

# Shiprocket tokens are valid for ~24h; cache for 6h to be safe
_SHIPROCKET_TOKEN_TTL_SECONDS = 6 * 3600

logger = logging.getLogger(__name__)

class ShiprocketLogisticsAdapter(LogisticsInterface):
    def __init__(self, client_id: str = None):
        self.client_id = client_id
        self._config: Optional[Dict[str, Any]] = None
        self._order_prefix: Optional[str] = None
        self._auth_token: Optional[str] = None
        self._auth_token_ts: float = 0.0

    @classmethod
    async def create(cls, client_id: str = None) -> "ShiprocketLogisticsAdapter":
        """Factory method that eagerly loads config + order_prefix once."""
        adapter = cls(client_id=client_id)
        adapter._config = await aget_shiprocket_config(client_id=client_id)
        try:
            prefix = await aget_config('order_prefix', client_id=client_id, default=None)
            adapter._order_prefix = prefix.lower() if prefix else ""
        except Exception:
            adapter._order_prefix = ""
        return adapter

    async def _aget_config(self, state: Optional[Dict] = None):
        # Truthy (not ``is not None``) guard: when the adapter was built with a
        # client_id=None it eagerly cached an empty config; allow a later call
        # that can resolve a real client_id (via state / ContextVar) to retry.
        if self._config:
            return self._config
        client_id = self._get_client_id(state)
        self._config = await aget_shiprocket_config(client_id=client_id)
        return self._config

    def _get_client_id(self, state: Optional[Dict] = None) -> Optional[str]:
        if self.client_id:
            return self.client_id
        if state and state.get("client_id"):
            return state.get("client_id")
        # Web/streaming tool calls don't always carry client_id in ``state``;
        # fall back to the request-scoped ContextVar (set in websocket_chat /
        # streaming_service) so config resolution doesn't collapse to None.
        from fashion_bot.client_context import get_client_id
        return get_client_id()

    def _clean_phone_for_shiprocket(self, phone: str) -> str:
        digits = re.sub(r"\D", "", str(phone or ""))
        if digits.startswith("91") and len(digits) > 10:
            digits = digits[2:]
        if digits.startswith("0") and len(digits) > 10:
            digits = digits[1:]
        return digits[-10:] if len(digits) >= 10 else digits

    def _is_valid_phone(self, phone: str) -> bool:
        return len(self._clean_phone_for_shiprocket(phone)) >= 10

    async def _build_channel_order_search(self, order_id: str, state: Optional[Dict] = None) -> str:
        if self._order_prefix is not None:
            order_prefix = self._order_prefix
        else:
            client_id = self._get_client_id(state)
            order_prefix = await aget_config("order_prefix", default=None, client_id=client_id) or ""

        order_name_clean = order_id.replace("#", "").lower()
        if order_prefix and order_name_clean.startswith(order_prefix.lower()):
            return order_name_clean.upper()
        if order_prefix:
            return f"{order_prefix.upper()}{order_name_clean.upper()}"
        return order_name_clean.upper()

    @staticmethod
    def _raise_sync_unavailable(method_name: str):
        raise RuntimeError(
            f"ShiprocketLogisticsAdapter.{method_name} is async-only. Use the corresponding `await a...` method."
        )

    async def _aauthenticate(self, api_base: str, email: str, password: str) -> Optional[str]:
        # Return cached token if still valid
        if self._auth_token and (time.time() - self._auth_token_ts) < _SHIPROCKET_TOKEN_TTL_SECONDS:
            return self._auth_token
        try:
            client = await get_shared_async_http_client()
            _t0 = time.monotonic()
            response = await client.post(
                f"{api_base}/auth/login",
                json={"email": email, "password": password},
                timeout=30,
            )
            logger.info(f"[SHIPROCKET] POST auth/login elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
            if response.status_code == 200:
                token = response.json().get("token")
                if token:
                    self._auth_token = token
                    self._auth_token_ts = time.time()
                return token
        except Exception as exc:
            logger.error(f"[SHIPROCKET] Auth failed: {exc}")
        return None

    async def aget_matching_orders(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> list[Dict[str, Any]]:
        search_query = await self._build_channel_order_search(order_id, state=state)
        try:
            return await self._aget_orders_from_search_query(search_query, state=state)
        except httpx.HTTPError as exc:
            log_with_trace_id(state, f"Error fetching Shiprocket orders: {exc}", "error")
            return []

    async def _aget_orders_from_search_query(
        self,
        query_string: str,
        state: Optional[Dict] = None,
    ) -> list[Dict[str, Any]]:
        config = await self._aget_config(state)
        api_base = config.get("api_base")
        email = config.get("email")
        password = config.get("password")
        if not api_base or not email or not password:
            return []

        token = await self._aauthenticate(api_base, email, password)
        if not token:
            return []

        client = await get_shared_async_http_client()
        headers = {"Authorization": f"Bearer {token}"}
        search_url = f"https://apiv2.shiprocket.co/v1/global/search?is_web=1&query_string={query_string}"

        _t0 = time.monotonic()
        search_response = await client.get(search_url, headers=headers, timeout=30)
        logger.info(f"[SHIPROCKET] GET search q={query_string} elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={search_response.status_code}")
        if search_response.status_code != 200:
            return []

        payload = search_response.json()
        entries = payload.get("data", {}).get("channel_order_id", []) if payload.get("success") else []
        min_year = datetime.now().year - 1
        orders: list[Dict[str, Any]] = []
        processed_order_ids = set()

        for entry in entries:
            order_numeric_id = entry.get("order_id")
            date_created = entry.get("date_created_at", "")
            if order_numeric_id in processed_order_ids:
                continue
            if date_created:
                try:
                    year = int(str(date_created).split("-")[0])
                    if year < min_year:
                        continue
                except (ValueError, IndexError):
                    pass

            processed_order_ids.add(order_numeric_id)
            _t1 = time.monotonic()
            detail_response = await client.get(
                f"{api_base}/orders/show/{order_numeric_id}",
                headers=headers,
                timeout=30,
            )
            logger.info(f"[SHIPROCKET] GET order/{order_numeric_id} elapsed_ms={int((time.monotonic() - _t1) * 1000)} status={detail_response.status_code}")
            if detail_response.status_code != 200:
                continue
            detail_data = detail_response.json().get("data", {})
            if not detail_data:
                continue
            # Run the raw Shiprocket detail payload through the central
            # field-map so the canonical contract is enforced in one place
            # (``GET_ORDER_DATA_FIELD_MAPS["shiprocket"]``) rather than
            # relying on undocumented passthrough.
            canonical = apply_order_data_mapping("shiprocket", detail_data)
            # Merge canonical fields onto the raw payload so downstream
            # code that still reads vendor-specific keys keeps working
            # during the migration window.
            merged = {**detail_data, **canonical}
            orders.append(
                {
                    "order_id": order_numeric_id,
                    "channel_order_id": entry.get("channel_order_id"),
                    "status": (detail_data.get("status") or "").upper(),
                    "order_data": merged,
                }
            )

        return orders

    async def aget_orders_by_phone(
        self,
        phone: str,
        state: Optional[Dict] = None,
    ) -> list[Dict[str, Any]]:
        cleaned_phone = self._clean_phone_for_shiprocket(phone)
        if not cleaned_phone:
            return []

        try:
            orders = await self._aget_orders_from_search_query(cleaned_phone, state=state)
        except httpx.HTTPError as exc:
            log_with_trace_id(state, f"Error searching Shiprocket orders by phone: {exc}", "error")
            return []

        matching_orders = []
        for order in orders:
            order_data = order.get("order_data", {}) or {}
            candidate_phone = (
                order_data.get("billing_phone")
                or order_data.get("billing_customer_phone")
                or order_data.get("customer_phone")
                or order_data.get("shipping_phone")
                or ""
            )
            if self._clean_phone_for_shiprocket(candidate_phone) == cleaned_phone:
                matching_orders.append(order)
        return matching_orders

    def _authenticate(self, api_base, email, password):
        self._raise_sync_unavailable("_authenticate")

    def get_tracking_details(self, tracking_number: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_tracking_details")

    async def aget_tracking_details(self, tracking_number: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShiprocketAdapter: Async getting tracking details for: {tracking_number}")

        config = await self._aget_config(state)
        api_base = config.get("api_base")
        email = config.get("email")
        password = config.get("password")
        if not api_base or not email or not password:
            log_with_trace_id(state, f"Shiprocket is not configured for client {self._get_client_id(state)}", "error")
            return {"status": "error", "message": "Shiprocket is not configured for this client"}

        token = await self._aauthenticate(api_base, email, password)
        if not token:
            log_with_trace_id(state, "Shiprocket authentication failed", "error")
            return {"status": "error", "message": "Authentication failed"}

        client = await get_shared_async_http_client()
        try:
            _t0 = time.monotonic()
            response = await client.get(
                f"{api_base}/courier/track/awb/{tracking_number}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            logger.info(f"[SHIPROCKET] GET track/awb/{tracking_number} elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
            if response.status_code != 200:
                log_with_trace_id(state, f"Shiprocket API error: {response.status_code}", "error")
                return {"status": "error", "message": f"API Error: {response.status_code}"}

            data = response.json().get("tracking_data", {})
            activities = data.get("shipment_track_activities", [])
            if not activities:
                return {
                    "awb": tracking_number,
                    "status": "No tracking updates logged yet",
                    "current_location": "N/A",
                    "latest_activity": "N/A",
                    "last_update_date": "N/A",
                }

            latest = activities[0]
            activity = latest.get("activity", "").upper()
            location = latest.get("location", "")
            raw_date = latest.get("date", "")
            return {
                "awb": tracking_number,
                "status": "Tracking available",
                "current_location": location,
                "latest_activity": activity,
                "last_update_date": raw_date,
                "formatted_update": f"📍 Last update: {activity} at {location} on {raw_date}",
            }
        except httpx.HTTPError as exc:
            log_with_trace_id(state, f"Error fetching tracking details: {exc}", "error")
            return {"status": "error", "message": str(exc)}

    def create_shipment(self, order_details: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        raise NotImplementedError()

    def cancel_shipment(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("cancel_shipment")

    async def acancel_shipment(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShiprocketAdapter: Async cancelling shipment for order {order_id}")
        config = await self._aget_config(state)
        api_base = config.get("api_base")
        email = config.get("email")
        password = config.get("password")
        if not api_base or not email or not password:
            log_with_trace_id(state, f"Shiprocket is not configured for client {self._get_client_id(state)}", "error")
            return {"success": False, "error": "Shiprocket is not configured for this client", "order_id": order_id}

        token = await self._aauthenticate(api_base, email, password)
        if not token:
            log_with_trace_id(state, "Shiprocket authentication failed", "error")
            return {"success": False, "error": "Authentication failed", "order_id": order_id}

        matching_orders = await self.aget_matching_orders(order_id, state=state)
        order_ids = [order.get("order_id") for order in matching_orders if order.get("order_id")]
        if not order_ids:
            return {"success": False, "error": "Order not found in logistics system", "order_id": order_id}

        client = await get_shared_async_http_client()
        try:
            response = await client.post(
                f"{api_base}/orders/cancel",
                json={"ids": order_ids},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if response.status_code in [200, 201, 202]:
                log_with_trace_id(state, f"Shiprocket shipment cancelled for order {order_id}")
                return {
                    "success": True,
                    "order_id": order_id,
                    "message": "Shipment cancelled in logistics system",
                }
            error_msg = response.text
            log_with_trace_id(state, f"Shiprocket cancellation failed: {error_msg}", "error")
            return {
                "success": False,
                "order_id": order_id,
                "error": f"Failed to cancel shipment: {error_msg}",
            }
        except httpx.HTTPError as exc:
            log_with_trace_id(state, f"Error cancelling shipment: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def get_delivery_estimate(
        self,
        pickup_pincode: str,
        destination_pincode: str, 
        weight: float = 0.5, 
        cod: bool = False, 
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Get estimated delivery time between two postal codes.
        
        Args:
            pickup_pincode: Warehouse or origin postal code
            destination_pincode: Customer / delivery postal code
            weight: Parcel weight in kilograms (default 0.5 kg)
            cod: Whether Cash-on-Delivery is required (default False)
            state: Optional state dictionary
            
        Returns:
            Dictionary with best courier, estimated delivery, and all options
        """
        import re
        
        self._raise_sync_unavailable("get_delivery_estimate")

    async def aget_delivery_estimate(
        self,
        pickup_pincode: str,
        destination_pincode: str,
        weight: float = 0.5,
        cod: bool = False,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShiprocketAdapter: Getting delivery estimate from {pickup_pincode} to {destination_pincode}")

        pickup_clean = pickup_pincode.strip()
        destination_clean = destination_pincode.strip()
        if not pickup_clean or not destination_clean:
            return {"status": "error", "message": "Both pickup and destination postal codes must be provided"}
        if not re.search(r"[A-Za-z0-9]", pickup_clean) or not re.search(r"[A-Za-z0-9]", destination_clean):
            return {"status": "error", "message": "Both postal codes must contain valid alphanumeric characters"}

        config = await self._aget_config(state)
        api_base = config.get("api_base")
        email = config.get("email")
        password = config.get("password")
        if not api_base or not email or not password:
            log_with_trace_id(state, f"Shiprocket is not configured for client {self._get_client_id(state)}", "error")
            return {"status": "error", "message": "Shiprocket is not configured for this client"}

        token = await self._aauthenticate(api_base, email, password)
        if not token:
            log_with_trace_id(state, "Shiprocket authentication failed", "error")
            return {"status": "error", "message": "Authentication failed"}

        client = await get_shared_async_http_client()
        try:
            response = await client.get(
                f"{api_base}/courier/serviceability",
                params={
                    "pickup_postcode": pickup_clean,
                    "delivery_postcode": destination_clean,
                    "weight": weight,
                    "cod": 1 if cod else 0,
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if response.status_code != 200:
                return {"status": "error", "message": f"API Error: {response.status_code}"}

            data = response.json()
            couriers = data.get("data", {}).get("available_courier_companies", [])
            if not couriers:
                return {"status": "error", "message": "No courier options available for the given postal codes"}

            def _parse_days(etd_str):
                if not etd_str:
                    return 999
                match = re.match(r"(\d+)", str(etd_str))
                return int(match.group(1)) if match else 999

            best = min(couriers, key=lambda c: _parse_days(c.get("etd", c.get("estimated_delivery_days", ""))))
            best_etd = best.get("etd") or best.get("estimated_delivery_days")
            return {
                "status": "success",
                "origin_pincode": pickup_pincode,
                "destination_pincode": destination_pincode,
                "best_courier": best.get("courier_name"),
                "estimated_delivery": best_etd,
                "all_options_count": len(couriers),
            }
        except Exception as e:
            log_with_trace_id(state, f"Error getting delivery estimate: {e}", "error")
            return {"status": "error", "message": str(e)}

    def update_shipment_address(self, order_id: str, address_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("update_shipment_address")

    async def aupdate_shipment_address(
        self,
        order_id: str,
        address_data: Dict[str, Any],
        state: Optional[Dict] = None,
        skip_status_check: bool = False,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShiprocketAdapter: Async updating address for order {order_id}")
        config = await self._aget_config(state)
        api_base = config.get("api_base")
        email = config.get("email")
        password = config.get("password")
        if not api_base or not email or not password:
            log_with_trace_id(state, f"Shiprocket is not configured for client {self._get_client_id(state)}", "error")
            return {"success": False, "error": "Shiprocket is not configured for this client", "order_id": order_id}

        token = await self._aauthenticate(api_base, email, password)
        if not token:
            log_with_trace_id(state, "Shiprocket authentication failed", "error")
            return {"success": False, "error": "Authentication failed", "order_id": order_id}

        matching_orders = await self.aget_matching_orders(order_id, state=state)
        if not matching_orders:
            return {"success": False, "error": "Order not found in Shiprocket", "order_id": order_id}

        def score_order(order: Dict[str, Any]) -> int:
            data = order.get("order_data", {}) or {}
            score = 0
            phone = (
                data.get("billing_phone")
                or data.get("billing_customer_phone")
                or data.get("customer_phone")
                or data.get("shipping_phone")
                or ""
            )
            if self._is_valid_phone(phone):
                score += 100
            if (data.get("billing_customer_name") or data.get("customer_name") or "").strip():
                score += 50
            if (data.get("billing_address") or data.get("customer_address") or "").strip():
                score += 25
            if (data.get("billing_city") or data.get("customer_city") or "").strip():
                score += 10
            return score

        # Statuses where address update should be skipped (order already in transit/delivered/etc.)
        skip_statuses = {
            "DELIVERED", "CANCELED",
            "IN TRANSIT", "IN TRANSIT-EN-ROUTE", "OUT FOR DELIVERY", "PICKED UP",
            "MISROUTED", "UNDELIVERED-1ST ATTEMPT", "UNDELIVERED-2ND ATTEMPT",
            "UNDELIVERED-3RD ATTEMPT", "UNDELIVERED", "REACHED AT DESTINATION HUB", "SHIPPED",
        }
        # Statuses where we need to cancel and recreate the order with new address
        special_pickup_statuses = {
            "PICKUP SCHEDULED", "PICKUP RESCHEDULED", "PICKUP EXCEPTION", "OUT FOR PICKUP",
        }

        errors = []
        updated = []
        skipped = []
        recreated = []

        for order in matching_orders:
            oid = order.get("order_id")
            status = (order.get("status") or "").upper()
            coid = order.get("channel_order_id", "")

            if status in skip_statuses and not skip_status_check:
                skipped.append({"channel_order_id": coid, "order_id": oid, "status": status})
                log_with_trace_id(state, f"Skipping address update for order {oid} (Status: {status})")
                continue

            if status in special_pickup_statuses and not skip_status_check:
                # Cancel and recreate with new address
                log_with_trace_id(state, f"Order {oid} in special status '{status}', cancelling and recreating")
                try:
                    client = await get_shared_async_http_client()
                    # Cancel the order
                    cancel_resp = await client.post(
                        f"{api_base}/orders/cancel",
                        json={"ids": [int(oid)]},
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        timeout=30,
                    )
                    # Wait for Shiprocket to process cancellation
                    await asyncio.sleep(3)
                    # Fetch full order details for recreation
                    detail_resp = await client.get(
                        f"{api_base}/orders/show/{oid}",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=30,
                    )
                    existing_order_data = detail_resp.json().get("data", {}) if detail_resp.status_code == 200 else {}
                    # Build new shipping dict
                    new_shipping = {
                        "shipping_customer_name": address_data.get("name", ""),
                        "shipping_address": address_data.get("address1") or address_data.get("address", ""),
                        "shipping_address_2": address_data.get("address2", ""),
                        "shipping_city": address_data.get("city", ""),
                        "shipping_state": address_data.get("state", ""),
                        "shipping_pincode": str(address_data.get("zip", "")),
                        "shipping_phone": address_data.get("phone", ""),
                        "shipping_country": address_data.get("country", "India"),
                    }
                    from fashion_bot.shopify.modules.create_shiprocket_order import acreate_shiprocket_order
                    create_resp = await acreate_shiprocket_order(token, existing_order_data, new_shipping)
                    recreated.append({"cancelled": oid, "created": create_resp})
                except Exception as exc:
                    log_with_trace_id(state, f"Cancel-and-recreate failed for order {oid}: {exc}", "error")
                    errors.append({"order_id": oid, "error": f"Cancel-and-recreate failed: {exc}"})
                continue

            # Normal address update for this order
            sr_order_data = order.get("order_data", {}) or {}
            existing_name = sr_order_data.get("billing_customer_name") or sr_order_data.get("customer_name") or ""
            existing_address = sr_order_data.get("billing_address") or sr_order_data.get("customer_address") or ""
            existing_address2 = sr_order_data.get("billing_address_2") or ""
            existing_city = sr_order_data.get("billing_city") or sr_order_data.get("customer_city") or ""
            existing_state_val = sr_order_data.get("billing_state") or sr_order_data.get("customer_state") or ""
            existing_pincode = sr_order_data.get("billing_pincode") or sr_order_data.get("customer_pincode") or ""
            existing_phone = (
                sr_order_data.get("billing_phone")
                or sr_order_data.get("billing_customer_phone")
                or sr_order_data.get("customer_phone")
                or sr_order_data.get("shipping_phone")
                or ""
            )
            existing_email = sr_order_data.get("billing_email") or sr_order_data.get("customer_email") or ""
            existing_country = sr_order_data.get("billing_country") or sr_order_data.get("customer_country") or "India"

            new_phone = self._clean_phone_for_shiprocket(address_data.get("phone", ""))
            cleaned_existing_phone = self._clean_phone_for_shiprocket(existing_phone)
            state_phone = self._clean_phone_for_shiprocket(state.get("phone_number", "") if state else "")
            resolved_phone = new_phone or cleaned_existing_phone or state_phone

            update_payload = {
                "order_id": oid,
                "shipping_customer_name": address_data.get("name") or existing_name,
                "shipping_address": address_data.get("address1") or address_data.get("address") or existing_address,
                "shipping_address_2": address_data.get("address2", existing_address2),
                "shipping_city": address_data.get("city") or existing_city,
                "shipping_state": address_data.get("state") or existing_state_val,
                "shipping_pincode": address_data.get("zip") or existing_pincode,
                "shipping_phone": resolved_phone,
                "shipping_email": address_data.get("email") or existing_email,
                "shipping_country": address_data.get("country") or existing_country,
            }

            required_fields = [
                "shipping_customer_name",
                "shipping_address",
                "shipping_city",
                "shipping_country",
                "shipping_phone",
            ]
            missing_fields = [field for field in required_fields if not update_payload.get(field)]
            if missing_fields:
                errors.append({
                    "order_id": oid,
                    "error": f"Missing required shipping fields: {', '.join(missing_fields)}",
                })
                continue

            client = await get_shared_async_http_client()
            try:
                response = await client.post(
                    f"{api_base}/orders/address/update",
                    json=update_payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    timeout=30,
                )
                if response.status_code in [200, 201, 202]:
                    updated.append({"channel_order_id": coid, "order_id": oid})
                else:
                    errors.append({
                        "order_id": oid,
                        "error": f"Update failed: {response.text or f'HTTP {response.status_code}'}",
                    })
            except httpx.HTTPError as exc:
                log_with_trace_id(state, f"Error updating address for order {oid}: {exc}", "error")
                errors.append({"order_id": oid, "error": str(exc)})

        # Return results matching old behavior
        if skipped:
            return {
                "success": False,
                "skipped": skipped,
                "message": "Someone from our team will contact you soon.",
                "order_id": order_id,
            }
        if recreated:
            created_status = (recreated[0].get("created") or {}).get("status")
            return {
                "success": created_status == 1,
                "shiprocket": recreated,
                "order_id": order_id,
                "message": "Shiprocket order cancelled and re-created due to pickup-stage status.",
            }
        if errors:
            return {"success": False, "updated": updated, "errors": errors, "order_id": order_id}
        return {"success": True, "updated": updated, "order_id": order_id, "message": "Address updated in Shiprocket"}

    def update_shipment_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Update phone number for a shipment in Shiprocket.
        
        Args:
            order_id: Order ID
            new_phone: New phone number
            state: Optional state dictionary
            
        Returns:
            Dictionary with update result
        """
        # Delegate to update_shipment_address with phone field
        self._raise_sync_unavailable("update_shipment_phone")

    async def aupdate_shipment_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        return await self.aupdate_shipment_address(order_id, {"phone": new_phone}, state)

    def update_shipment_email(self, order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Update email for a shipment in Shiprocket.
        
        Args:
            order_id: Order ID
            new_email: New email address
            state: Optional state dictionary
            
        Returns:
            Dictionary with update result
        """
        # Delegate to update_shipment_address with email field
        self._raise_sync_unavailable("update_shipment_email")

    async def aupdate_shipment_email(self, order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        return await self.aupdate_shipment_address(order_id, {"email": new_email}, state, skip_status_check=True)

    def update_shipment_name(self, order_id: str, first_name: str, last_name: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Update customer name (shipping_customer_name) for a shipment in Shiprocket.
        
        Shiprocket uses the `shipping_customer_name` field in the order address update API.
        Delegates to update_shipment_address with just the name field so that all existing
        address fields are preserved.
        
        Args:
            order_id: Order ID
            first_name: New first name
            last_name: New last name
            state: Optional state dictionary
            
        Returns:
            Dictionary with update result
        """
        self._raise_sync_unavailable("update_shipment_name")

    async def aupdate_shipment_name(
        self,
        order_id: str,
        first_name: str,
        last_name: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        full_name = f"{first_name} {last_name}".strip()
        log_with_trace_id(state, f"ShiprocketAdapter: Async updating shipment name for order {order_id} to '{full_name}'")
        return await self.aupdate_shipment_address(order_id, {"name": full_name}, state)

    def get_order_data(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_order_data")

    async def aget_order_data(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        try:
            orders = await self.aget_matching_orders(order_id, state=state)
            if not orders:
                return {
                    "success": False,
                    "found": False,
                    "message": f"Order {order_id} not found in logistics system",
                }

            order = orders[0]
            order_data = order.get("order_data", {})
            return {
                "success": True,
                "found": True,
                "order_id": order_id,
                "logistics_order_id": order.get("order_id"),
                "status": order.get("status"),
                "order_data": order_data,
                "shipments": order_data.get("shipments", {}),
                "delivered_on": order_data.get("shipments", {}).get("delivered_on")
                if order_data.get("shipments")
                else None,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}
