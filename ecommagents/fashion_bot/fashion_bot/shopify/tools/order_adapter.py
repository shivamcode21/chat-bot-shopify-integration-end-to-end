import logging
import re
import time
import urllib.parse
from typing import Dict, Any, Optional, List
import httpx
from fashion_bot.interfaces.order import OrderInterface
from fashion_bot.tool_helpers import ShopifyRateLimitError
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.config_manager import (
    BLOOMERCE_INTEGRATION_REQUIRED_MESSAGE,
    aget_shopify_config,
    aget_config,
)
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.product_utils import (
    variant_is_available,
    resolve_variant_match,
    find_variant_by_id,
)

logger = logging.getLogger(__name__)

class ShopifyOrderAdapter(OrderInterface):
    def __init__(self, client_id: str = None):
        self.client_id = client_id
        self._config: Optional[Dict[str, Any]] = None
        self._order_prefix: Optional[str] = None

    @classmethod
    async def create(cls, client_id: str = None) -> "ShopifyOrderAdapter":
        """Factory method that eagerly loads config + order_prefix once."""
        adapter = cls(client_id=client_id)
        adapter._config = await aget_shopify_config(client_id=client_id)
        try:
            prefix = await aget_config('order_prefix', client_id=client_id, default=None)
            adapter._order_prefix = prefix.lower() if prefix else ""
        except Exception:
            adapter._order_prefix = ""
        return adapter

    async def _aget_config(self, state: Optional[Dict] = None):
        if self._config is not None:
            return self._config
        client_id = self.client_id
        if not client_id and state:
            client_id = state.get('client_id')
        self._config = await aget_shopify_config(client_id=client_id)
        return self._config

    @staticmethod
    def _heal_shipping_address(
        shipping: Dict[str, Any],
        state: Optional[Dict] = None,
        order_id: str = "",
    ) -> Dict[str, str]:
        """Return address field corrections for known garbled-data patterns.

        Shopify validates the full merged shipping address on every update.
        Orders with garbled data (e.g. PIN code in ``province``, state name in
        ``city``) will 422 even for unrelated changes.  This helper detects
        those patterns and returns a patch dict that callers should merge into
        the outgoing ``shipping_address`` payload.

        Returns an empty dict when no healing is needed.
        """
        patch: Dict[str, str] = {}
        province = (shipping.get("province") or "").strip()
        city = (shipping.get("city") or "").strip()
        if province and province.isdigit() and city and not city.isdigit():
            patch["province"] = city
            if state is not None:
                log_with_trace_id(
                    state,
                    f"🔧 Healing garbled province for {order_id}: "
                    f"'{province}' → '{city}'",
                )
        return patch

    @staticmethod
    def _missing_integration_error() -> str:
        return BLOOMERCE_INTEGRATION_REQUIRED_MESSAGE

    def _get_client_id(self, state: Optional[Dict] = None):
        if self.client_id:
            return self.client_id
        if state:
            return state.get('client_id')
        return None

    async def _get_order_prefix(self, state: Optional[Dict] = None) -> str:
        """Get order prefix from configuration.

        Returns:
            The configured order prefix, or empty string if not configured.
            Never returns a hardcoded default - prefix must be configured in DB.
        """
        if self._order_prefix is not None:
            return self._order_prefix
        client_id = self._get_client_id(state)
        try:
            prefix = await aget_config('order_prefix', client_id=client_id, default=None)
            if prefix is None:
                logger.warning(f"order_prefix NOT CONFIGURED for client {client_id}")
                self._order_prefix = ""
                return ""
            self._order_prefix = prefix.lower() if prefix else ""
            return self._order_prefix
        except Exception as e:
            logger.warning(f"Failed to get order_prefix: {e}")
            self._order_prefix = ""
            return ""

    async def _normalize_order_id_variants(self, input_text: str, state: Optional[Dict] = None) -> List[str]:
        prefix = await self._get_order_prefix(state)
        # Remove generic prefixes
        cleaned = input_text.lower().replace('order', '').replace('#', '').strip()
        
        variants = [cleaned]
        if prefix and not cleaned.startswith(prefix):
            variants.append(f"{prefix}{cleaned}")
        if not cleaned.startswith('#'):
            variants.append(f"#{cleaned}")
            if prefix and not cleaned.startswith(prefix):
                variants.append(f"#{prefix}{cleaned}")
        
        # Also add Upper case variants
        variants += [v.upper() for v in variants]
        return list(set(variants))

    def _get_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }

    def _normalize_phone_value(self, phone: str) -> str:
        """Normalize to E.164 with +91 prefix for Shopify API compatibility."""
        cleaned = str(phone or "")
        if cleaned.startswith("+91"):
            cleaned = cleaned[3:]
        elif len(cleaned) > 10 and cleaned.startswith("91"):
            cleaned = cleaned[2:]
        digits = re.sub(r"\D", "", cleaned)
        if digits.startswith("91") and len(digits) > 10:
            digits = digits[2:]
        bare = digits[-10:] if len(digits) >= 10 else digits
        if len(bare) == 10:
            return f"+91{bare}"
        return bare

    @staticmethod
    def _raise_for_shopify_response(response: httpx.Response) -> None:
        if response.status_code == 429:
            retry_after_raw = response.headers.get("Retry-After", 2.0)
            try:
                retry_after = float(retry_after_raw)
            except (TypeError, ValueError):
                retry_after = 2.0
            raise ShopifyRateLimitError(retry_after=retry_after)
        response.raise_for_status()

    @staticmethod
    def _raise_sync_unavailable(method_name: str):
        raise RuntimeError(
            f"ShopifyOrderAdapter.{method_name} is async-only. Use the corresponding `await a...` method."
        )

    async def _asearch_orders_by_variant(
        self,
        variant: str,
        config: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        client = await get_shared_async_http_client()
        shop_url = config.get("shop_url")
        if not shop_url:
            return []

        api_version = config.get("api_version", "2024-04")
        name = variant.lstrip("#")
        encoded_order_name = f"%23{name}"
        url = f"https://{shop_url}/admin/api/{api_version}/orders.json?name={encoded_order_name}&status=any"

        log_with_trace_id(state, f"ShopifyAdapter: Async trying order lookup: {url}")
        _t0 = time.monotonic()
        response = await client.get(
            url,
            headers={"X-Shopify-Access-Token": config.get("access_token", "")},
            timeout=30,
        )
        logger.info(f"[SHOPIFY] GET order_lookup elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
        self._raise_for_shopify_response(response)
        return response.json().get("orders", [])

    async def _aresolve_order_record(
        self,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        config = await self._aget_config(state)
        access_token = config.get("access_token")
        shop_url = config.get("shop_url")
        api_version = config.get("api_version", "2024-04")

        if not access_token or not shop_url:
            raise ValueError(self._missing_integration_error())

        variants = await self._normalize_order_id_variants(order_id, state)
        client = await get_shared_async_http_client()

        for variant in variants:
            try:
                orders = await self._asearch_orders_by_variant(variant, config, state=state)
            except ShopifyRateLimitError:
                raise
            except httpx.HTTPError as exc:
                log_with_trace_id(state, f"Error fetching from Shopify: {exc}", "error")
                continue

            name = variant.lstrip("#")
            for order in orders:
                shopify_name = order.get("name", "").lower().lstrip("#")
                if shopify_name == name.lower():
                    return order
                if len(name) > 2 and shopify_name.startswith(name.lower() + "-exc"):
                    return order

        if str(order_id).isdigit():
            detail_url = f"https://{shop_url}/admin/api/{api_version}/orders/{order_id}.json"
            try:
                _t0 = time.monotonic()
                response = await client.get(
                    detail_url,
                    headers=self._get_headers(access_token),
                    timeout=30,
                )
                logger.info(f"[SHOPIFY] GET order_lookup_by_id elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
                if response.status_code == 429:
                    self._raise_for_shopify_response(response)
                if response.status_code == 200:
                    order = response.json().get("order")
                    if order:
                        return order
            except ShopifyRateLimitError:
                raise
            except httpx.HTTPError as exc:
                log_with_trace_id(state, f"Error fetching Shopify order by ID: {exc}", "error")

        return {}

    async def _aorder_put(
        self,
        numeric_order_id: str,
        payload: Dict[str, Any],
        config: Dict[str, Any],
    ) -> httpx.Response:
        client = await get_shared_async_http_client()
        url = f"https://{config['shop_url']}/admin/api/{config.get('api_version', '2024-04')}/orders/{numeric_order_id}.json"
        _t0 = time.monotonic()
        response = await client.put(
            url,
            json=payload,
            headers=self._get_headers(config["access_token"]),
            timeout=30,
        )
        logger.info(f"[SHOPIFY] PUT order_update elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
        self._raise_for_shopify_response(response)
        return response

    async def _aorder_update_graphql(
        self,
        numeric_order_id: str,
        config: Dict[str, Any],
        shipping_address: Optional[Dict[str, Any]] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        mutation = """
        mutation OrderUpdate($input: OrderInput!) {
          orderUpdate(input: $input) {
            order {
              id
              note
              shippingAddress {
                firstName
                lastName
                address1
                address2
                city
                province
                zip
                country
                phone
              }
            }
            userErrors {
              field
              message
            }
          }
        }
        """

        input_obj: Dict[str, Any] = {"id": f"gid://shopify/Order/{numeric_order_id}"}
        if shipping_address:
            input_obj["shippingAddress"] = {
                key: value
                for key, value in shipping_address.items()
                if value is not None and (value != "" or key == "address2")
            }
        if note:
            input_obj["note"] = note

        client = await get_shared_async_http_client()
        url = f"https://{config['shop_url']}/admin/api/{config.get('api_version', '2024-04')}/graphql.json"
        _t0 = time.monotonic()
        response = await client.post(
            url,
            json={"query": mutation, "variables": {"input": input_obj}},
            headers=self._get_headers(config["access_token"]),
            timeout=30,
        )
        logger.info(f"[SHOPIFY] POST graphql elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
        self._raise_for_shopify_response(response)
        payload = response.json()
        if payload.get("errors"):
            return {"success": False, "error": "GraphQL error", "details": payload["errors"]}
        order_update = payload.get("data", {}).get("orderUpdate", {})
        user_errors = order_update.get("userErrors", [])
        if user_errors:
            error_msgs = "; ".join(f"{e.get('field', '?')}: {e.get('message', '?')}" for e in user_errors)
            logger.warning(f"[SHOPIFY] orderUpdate userErrors: {error_msgs}")
            return {"success": False, "error": f"Shopify order update failed: {error_msgs}", "details": user_errors}
        return {"success": True, "order": order_update.get("order"), "details": order_update}

    def get_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_order_details")

    async def aget_order_details(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        try:
            return await self._aresolve_order_record(order_id, state=state)
        except ShopifyRateLimitError:
            raise
        except Exception as exc:
            log_with_trace_id(state, f"Error fetching from Shopify: {exc}", "error")
            return {}

    async def aget_order_transactions(
        self, numeric_order_id: Any, state: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch REST transactions for an order by its numeric Shopify id.

        Returns ``[]`` on any failure (fail-open) so callers that only need the
        transactions for annotation never break the main flow. Issues the same
        endpoint the refund flow uses in ``arefund_order``.
        """
        try:
            config = await self._aget_config(state)
            access_token = config.get("access_token")
            shop_url = config.get("shop_url")
            api_version = config.get("api_version", "2024-04")
            if not access_token or not shop_url or not numeric_order_id:
                return []
            client = await get_shared_async_http_client()
            url = (
                f"https://{shop_url}/admin/api/{api_version}"
                f"/orders/{numeric_order_id}/transactions.json"
            )
            _t0 = time.monotonic()
            response = await client.get(
                url, headers=self._get_headers(access_token), timeout=30,
            )
            logger.info(f"[SHOPIFY] GET order_transactions elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
            self._raise_for_shopify_response(response)
            return response.json().get("transactions", [])
        except ShopifyRateLimitError:
            raise
        except Exception as exc:
            log_with_trace_id(state, f"Error fetching transactions for {numeric_order_id}: {exc}", "warning")
            return []

    def get_orders_by_customer_phone(self, phone: str, limit: int = 50, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        self._raise_sync_unavailable("get_orders_by_customer_phone")

    async def aget_orders_by_customer_phone(
        self,
        phone: str,
        limit: int = 50,
        state: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        config = await self._aget_config(state)
        access_token = config.get("access_token")
        shop_url = config.get("shop_url")
        api_version = config.get("api_version", "2024-04")

        if not access_token or not shop_url:
            client_id = self._get_client_id(state)
            log_with_trace_id(
                state,
                f"[SHOPIFY_CONFIG_MISSING] op=aget_orders_by_customer_phone "
                f"client_id={client_id} reason=no shopify_details in client_configs "
                f"(access_token={'present' if access_token else 'missing'}, "
                f"shop_url={'present' if shop_url else 'missing'})",
                "error",
            )
            return []

        client = await get_shared_async_http_client()
        headers = {"X-Shopify-Access-Token": access_token}

        # Shopify stores phones in E.164 (e.g. +919012345677).
        # Build variants so we match regardless of how the caller formats it.
        digits = re.sub(r"\D", "", phone)
        bare = digits[-10:] if len(digits) >= 10 else digits
        phone_variants = [f"+91{bare}", f"91{bare}", bare]

        try:
            # Collect ALL unique customer IDs across phone variants.
            # GoKwik and other channels often create duplicate customer
            # records, so the same phone can map to multiple customer IDs.
            seen_customer_ids = set()
            for variant in phone_variants:
                url = f"https://{shop_url}/admin/api/{api_version}/customers/search.json?query=phone:{variant}"
                log_with_trace_id(state, f"ShopifyAdapter: Async searching customer by phone: {variant}")
                _t0 = time.monotonic()
                response = await client.get(url, headers=headers, timeout=30)
                logger.info(f"[SHOPIFY] GET customer_search elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
                if response.status_code != 200:
                    log_with_trace_id(state, f"Shopify API error: {response.status_code} - {response.text}", "error")
                    continue
                for cust in response.json().get("customers", []):
                    seen_customer_ids.add(cust["id"])
                if seen_customer_ids:
                    break

            if not seen_customer_ids:
                log_with_trace_id(state, f"No customer found with phone number {phone} (tried {phone_variants})", "warning")
                return []

            log_with_trace_id(state, f"Found {len(seen_customer_ids)} customer record(s) for phone {phone}")

            all_orders = []
            seen_order_ids = set()
            for customer_id in seen_customer_ids:
                orders_url = (
                    f"https://{shop_url}/admin/api/{api_version}/orders.json"
                    f"?customer_id={customer_id}&status=any&limit={limit}"
                )
                _t1 = time.monotonic()
                orders_response = await client.get(orders_url, headers=headers, timeout=30)
                logger.info(f"[SHOPIFY] GET customer_orders elapsed_ms={int((time.monotonic() - _t1) * 1000)} status={orders_response.status_code}")
                self._raise_for_shopify_response(orders_response)
                for order in orders_response.json().get("orders", []):
                    oid = order.get("id")
                    if oid not in seen_order_ids:
                        seen_order_ids.add(oid)
                        all_orders.append(order)

            all_orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
            trimmed = all_orders[:limit]
            log_with_trace_id(state, f"✅ Found {len(all_orders)} total orders across {len(seen_customer_ids)} customer record(s), returning top {len(trimmed)}")
            return trimmed
        except ShopifyRateLimitError:
            raise
        except httpx.HTTPError as exc:
            log_with_trace_id(state, f"Error fetching customer orders: {exc}", "error")
            return []

    def create_order(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("create_order")

    async def acreate_order(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        from fashion_bot.shopify.modules.order_creation_api import (
            OrderCreationAPI,
            CustomerInfo,
            ProductSelection,
            ShippingAddress,
            ShopifyEnvironment,
            aget_discount_details,
        )
        from fashion_bot.shopify.modules.product_handlers import ashopify_get_product_by_handle_graphql

        log_with_trace_id(state, "ShopifyAdapter: Async creating order")

        try:
            config = await self._aget_config(state)
            access_token = config.get("access_token")
            shop_url = config.get("shop_url")
            api_version = config.get("api_version", "2024-04")

            if not access_token or not shop_url:
                return {"success": False, "error": self._missing_integration_error()}

            product_link = order_data.get("product_link", "")
            quantity = int(order_data.get("quantity", 1))
            requested_size = str(order_data.get("requested_size", ""))
            phone_number = str(order_data.get("phone_number", ""))
            customer_name = str(order_data.get("customer_name", ""))
            customer_address = str(order_data.get("customer_address", ""))
            financial_status = order_data.get("financial_status")
            payment_gateway_names = order_data.get("payment_gateway_names")
            note = order_data.get("note")
            transactions = order_data.get("transactions")

            handle = self._extract_handle_from_url(product_link)
            if not handle:
                return {"success": False, "error": f"Could not extract product handle from URL: {product_link}"}

            product_result = await ashopify_get_product_by_handle_graphql(
                handle=handle,
                access_token=access_token,
                shop_url=shop_url,
                api_version=api_version,
                formatted_response=False,
            )
            if not product_result.get("success") or not product_result.get("product"):
                return {"success": False, "error": f"Product not found: {handle}"}

            product_data = product_result["product"]
            product_id = str(product_data.get("id", "")).replace("gid://shopify/Product/", "")
            variant_result = self._find_variant_by_size(product_data, requested_size, state=state)
            if not variant_result.get("success"):
                return variant_result

            variant_id = variant_result["variant_id"]
            name_parts = customer_name.strip().split(" ", 1)
            first_name = name_parts[0] if name_parts else "Customer"
            last_name = name_parts[1] if len(name_parts) > 1 else "."

            address_components = self._parse_address(customer_address)
            clean_phone = self._normalize_phone_value(phone_number)
            email_phone = re.sub(r"\D", "", clean_phone) or "0000000000"
            customer_email = f"customer{email_phone}@ecommbot.com"

            client_id = self._get_client_id(state)
            discount_code, _ = await aget_discount_details(client_id)
            shopify_env = ShopifyEnvironment(
                access_token=access_token,
                shop_url=shop_url,
                api_version=api_version,
            )

            customer = CustomerInfo(email=customer_email, phone=clean_phone)
            address = ShippingAddress(
                first_name=first_name,
                last_name=last_name,
                address1=address_components.get("address1", customer_address),
                address2=address_components.get("address2", ""),
                city=address_components.get("city", ""),
                state=address_components.get("state", ""),
                zip_code=address_components.get("zip_code", ""),
                phone=clean_phone,
                country=address_components.get("country", "India"),
            )
            product_selection = ProductSelection(
                product_id=product_id,
                variant_id=variant_id,
                quantity=quantity,
            )

            api = OrderCreationAPI(shopify_env)
            result = await api.acreate_order(
                customer,
                address,
                product_selection,
                discount_code,
                financial_status=financial_status,
                payment_gateway_names=payment_gateway_names,
                note=note,
                transactions=transactions,
            )

            if not result.get("success"):
                return result

            order = result.get("order") or {}
            shipping_addr = order.get("shipping_address") or {}
            line_items = order.get("line_items") or []
            first_item = line_items[0] if line_items else {}
            order_name = order.get("name", "N/A")

            return {
                "success": True,
                "order_id": order_name,
                "order_number": order.get("order_number", ""),
                "total_price": order.get("total_price", ""),
                "subtotal_price": order.get("subtotal_price", ""),
                "currency": order.get("currency", "INR"),
                "financial_status": order.get("financial_status", "pending"),
                "fulfillment_status": order.get("fulfillment_status") or "unfulfilled",
                "customer_name": (
                    f"{shipping_addr.get('first_name', '')} {shipping_addr.get('last_name', '')}".strip()
                    or customer_name
                ),
                "customer_phone": shipping_addr.get("phone", "") or phone_number,
                "customer_email": order.get("email", ""),
                "shipping_address": {
                    "address1": shipping_addr.get("address1", ""),
                    "address2": shipping_addr.get("address2", ""),
                    "city": shipping_addr.get("city", ""),
                    "province": shipping_addr.get("province", ""),
                    "zip": shipping_addr.get("zip", ""),
                    "country": shipping_addr.get("country", "India"),
                    "phone": shipping_addr.get("phone", ""),
                },
                "product_name": first_item.get("title", ""),
                "quantity": first_item.get("quantity", 1),
                "message": f"Order {order_name} created successfully!",
            }
        except Exception as exc:
            log_with_trace_id(state, f"Error creating order: {exc}", "error")
            return {"success": False, "error": str(exc)}

    async def acreate_order_multi(self, order_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a SINGLE Shopify order containing multiple line items.

        ``order_data["items"]`` is a list of ``{product_link, requested_size,
        quantity, variant_id}`` dicts (``variant_id`` optional); customer/address
        fields are shared across all items. Each item is resolved to a variant —
        by ``variant_id`` when the caller knows it, else by size label — before the
        order is created, so a multi-item cart becomes one order instead of one
        per item.
        """
        from fashion_bot.shopify.modules.order_creation_api import (
            OrderCreationAPI,
            CustomerInfo,
            ProductSelection,
            ShippingAddress,
            ShopifyEnvironment,
            aget_discount_details,
        )
        from fashion_bot.shopify.modules.product_handlers import ashopify_get_product_by_handle_graphql

        log_with_trace_id(state, "ShopifyAdapter: Async creating multi-item order")

        try:
            config = await self._aget_config(state)
            access_token = config.get("access_token")
            shop_url = config.get("shop_url")
            api_version = config.get("api_version", "2024-04")

            if not access_token or not shop_url:
                return {"success": False, "error": self._missing_integration_error()}

            items = order_data.get("items") or []
            if not items:
                return {"success": False, "error": "No items provided for multi-item order"}

            phone_number = str(order_data.get("phone_number", ""))
            customer_name = str(order_data.get("customer_name", ""))
            customer_address = str(order_data.get("customer_address", ""))
            financial_status = order_data.get("financial_status")
            payment_gateway_names = order_data.get("payment_gateway_names")
            note = order_data.get("note")
            transactions = order_data.get("transactions")

            # Resolve every item's product link → variant before creating the order.
            product_selections = []
            for item in items:
                product_link = str(item.get("product_link", ""))
                requested_size = str(item.get("requested_size", "") or item.get("size", ""))
                requested_variant_id = str(item.get("variant_id", "") or "")
                quantity = int(item.get("quantity", 1) or 1)

                handle = self._extract_handle_from_url(product_link)
                if not handle:
                    return {"success": False, "error": f"Could not extract product handle from URL: {product_link}"}

                product_result = await ashopify_get_product_by_handle_graphql(
                    handle=handle,
                    access_token=access_token,
                    shop_url=shop_url,
                    api_version=api_version,
                    formatted_response=False,
                )
                if not product_result.get("success") or not product_result.get("product"):
                    return {"success": False, "error": f"Product not found: {handle}"}

                product_data = product_result["product"]
                product_id = str(product_data.get("id", "")).replace("gid://shopify/Product/", "")
                variant_result = self._find_variant_by_size(
                    product_data,
                    requested_size,
                    state=state,
                    requested_variant_id=requested_variant_id,
                )
                if not variant_result.get("success"):
                    return variant_result

                product_selections.append(ProductSelection(
                    product_id=product_id,
                    variant_id=variant_result["variant_id"],
                    quantity=quantity,
                ))

            name_parts = customer_name.strip().split(" ", 1)
            first_name = name_parts[0] if name_parts else "Customer"
            last_name = name_parts[1] if len(name_parts) > 1 else "."

            address_components = self._parse_address(customer_address)
            clean_phone = self._normalize_phone_value(phone_number)
            email_phone = re.sub(r"\D", "", clean_phone) or "0000000000"
            customer_email = f"customer{email_phone}@ecommbot.com"

            client_id = self._get_client_id(state)
            discount_code, _ = await aget_discount_details(client_id)
            shopify_env = ShopifyEnvironment(
                access_token=access_token,
                shop_url=shop_url,
                api_version=api_version,
            )

            customer = CustomerInfo(email=customer_email, phone=clean_phone)
            address = ShippingAddress(
                first_name=first_name,
                last_name=last_name,
                address1=address_components.get("address1", customer_address),
                address2=address_components.get("address2", ""),
                city=address_components.get("city", ""),
                state=address_components.get("state", ""),
                zip_code=address_components.get("zip_code", ""),
                phone=clean_phone,
                country=address_components.get("country", "India"),
            )

            api = OrderCreationAPI(shopify_env)
            result = await api.acreate_order_multi(
                customer,
                address,
                product_selections,
                discount_code,
                financial_status=financial_status,
                payment_gateway_names=payment_gateway_names,
                note=note,
                transactions=transactions,
            )

            if not result.get("success"):
                return result

            order = result.get("order") or {}
            shipping_addr = order.get("shipping_address") or {}
            line_items = order.get("line_items") or []
            order_name = order.get("name", "N/A")

            return {
                "success": True,
                "order_id": order_name,
                "order_number": order.get("order_number", ""),
                "total_price": order.get("total_price", ""),
                "subtotal_price": order.get("subtotal_price", ""),
                "currency": order.get("currency", "INR"),
                "financial_status": order.get("financial_status", "pending"),
                "fulfillment_status": order.get("fulfillment_status") or "unfulfilled",
                "customer_name": (
                    f"{shipping_addr.get('first_name', '')} {shipping_addr.get('last_name', '')}".strip()
                    or customer_name
                ),
                "customer_phone": shipping_addr.get("phone", "") or phone_number,
                "customer_email": order.get("email", ""),
                "shipping_address": {
                    "address1": shipping_addr.get("address1", ""),
                    "address2": shipping_addr.get("address2", ""),
                    "city": shipping_addr.get("city", ""),
                    "province": shipping_addr.get("province", ""),
                    "zip": shipping_addr.get("zip", ""),
                    "country": shipping_addr.get("country", "India"),
                    "phone": shipping_addr.get("phone", ""),
                },
                "line_items": [
                    {"title": li.get("title", ""), "quantity": li.get("quantity", 1), "price": li.get("price", "")}
                    for li in line_items
                ],
                "item_count": len(line_items),
                "message": f"Order {order_name} created successfully with {len(line_items)} item(s)!",
            }
        except Exception as exc:
            log_with_trace_id(state, f"Error creating multi-item order: {exc}", "error")
            return {"success": False, "error": str(exc)}

    async def aclone_order(
        self,
        original_order_data: Dict[str, Any],
        new_line_items: List[Dict[str, Any]],
        state: Optional[Dict] = None,
        note: str = "",
        additional_tags: Optional[List[str]] = None,
        financial_status_override: Optional[str] = None,
        payment_gateway_names_override: Optional[List[str]] = None,
        transactions: Optional[List[Dict[str, Any]]] = None,
        # ── Field-level overrides (introduced for the cancel-and-recreate
        # update strategy). Each one, when provided, is merged on top of
        # the original order's value — passing only the changed sub-fields
        # works (e.g. {"address1": "E-10 …", "zip": "110058"} keeps name,
        # phone, etc.). Pass ``None`` to inherit verbatim from original.
        shipping_address_override: Optional[Dict[str, Any]] = None,
        billing_address_override: Optional[Dict[str, Any]] = None,
        email_override: Optional[str] = None,
        phone_override: Optional[str] = None,
        customer_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Clone an order with new line items + optional field overrides.

        Copies shipping/billing address, customer, email, phone, discount
        codes, note_attributes, tax settings, and tags from the original
        order. Line items are replaced. Optional ``*_override`` arguments
        let callers swap individual fields (used by the cancel-and-recreate
        update strategy for address/phone/email/name changes).
        """
        log_with_trace_id(state, f"ShopifyAdapter: Cloning order {original_order_data.get('name', 'N/A')}")

        try:
            config = await self._aget_config(state)
            access_token = config.get("access_token")
            shop_url = config.get("shop_url")
            api_version = config.get("api_version", "2024-04")

            if not access_token or not shop_url:
                return {"success": False, "error": self._missing_integration_error()}

            shipping_address = dict(original_order_data.get("shipping_address") or {})
            billing_address = dict(
                original_order_data.get("billing_address")
                or original_order_data.get("shipping_address")
                or {}
            )
            customer_data = dict(original_order_data.get("customer") or {})
            customer_id = customer_data.get("id")

            # Field-level overrides for cancel-and-recreate flow. We merge
            # rather than replace so callers can pass just the changed
            # sub-fields (e.g. only address1 + zip without re-specifying
            # name + phone).
            if shipping_address_override:
                shipping_address.update(
                    {k: v for k, v in shipping_address_override.items() if v not in (None, "")}
                )
                # When province (state name) is overridden, drop the stale
                # province_code inherited from the original order. Shopify
                # gives province_code precedence — a leftover "DL" would
                # override province="Haryana" and resolve to Delhi.
                if "province" in shipping_address_override:
                    shipping_address.pop("province_code", None)
            if billing_address_override:
                billing_address.update(
                    {k: v for k, v in billing_address_override.items() if v not in (None, "")}
                )
                if "province" in billing_address_override:
                    billing_address.pop("province_code", None)
            elif shipping_address_override and original_order_data.get("billing_address") is None:
                # When no separate billing was on file, keep billing aligned
                # with the (now-edited) shipping. Otherwise the new order
                # ends up with a stale billing pincode/phone.
                billing_address.update(
                    {k: v for k, v in shipping_address_override.items() if v not in (None, "")}
                )
                if "province" in shipping_address_override:
                    billing_address.pop("province_code", None)
            if customer_override:
                customer_data.update(
                    {k: v for k, v in customer_override.items() if v not in (None, "")}
                )
            # Propagate name override to the address blocks too — Shopify
            # ships to whatever is on shipping_address.first_name /
            # last_name, not the customer record.
            if customer_override and (customer_override.get("first_name") or customer_override.get("last_name")):
                if customer_override.get("first_name"):
                    shipping_address["first_name"] = customer_override["first_name"]
                    billing_address["first_name"] = customer_override["first_name"]
                if customer_override.get("last_name"):
                    shipping_address["last_name"] = customer_override["last_name"]
                    billing_address["last_name"] = customer_override["last_name"]
            # Phone override goes to both the order-level and the address-
            # level fields (Shopify uses the address phone for shipment;
            # the order phone for SMS to customer).
            if phone_override:
                shipping_address["phone"] = phone_override
                billing_address.setdefault("phone", phone_override)

            order_name = original_order_data.get("name", "")
            heal_patch = self._heal_shipping_address(
                shipping_address, state=state, order_id=order_name,
            )
            if heal_patch:
                shipping_address.update(heal_patch)
                billing_address.update(heal_patch)
                if "province" in heal_patch:
                    shipping_address.pop("province_code", None)
                    billing_address.pop("province_code", None)

            original_tags = original_order_data.get("tags", "") or ""
            tag_list = [t.strip() for t in original_tags.split(",") if t.strip()]
            for t in (additional_tags or []):
                if t not in tag_list:
                    tag_list.append(t)
            combined_tags = ", ".join(tag_list)

            effective_financial_status = (
                financial_status_override
                or original_order_data.get("financial_status", "pending")
            )
            effective_gateways = (
                payment_gateway_names_override
                or original_order_data.get("payment_gateway_names")
                or ["Cash on Delivery (COD)"]
            )

            order_payload: Dict[str, Any] = {
                "line_items": [
                    {k: v for k, v in li.items() if k in ("variant_id", "quantity", "price", "title", "properties")}
                    for li in new_line_items
                ],
                "shipping_address": {
                    k: v for k, v in shipping_address.items()
                    if k in (
                        "first_name", "last_name", "company",
                        "address1", "address2", "city", "province",
                        "province_code", "zip", "country", "country_code",
                        "phone", "name",
                    ) and v
                },
                "financial_status": effective_financial_status,
                "payment_gateway_names": effective_gateways,
                "currency": original_order_data.get("currency", "INR"),
                "tags": combined_tags,
                "send_receipt": False,
                "send_fulfillment_receipt": False,
            }

            if billing_address and billing_address != shipping_address:
                order_payload["billing_address"] = {
                    k: v for k, v in billing_address.items()
                    if k in (
                        "first_name", "last_name", "company",
                        "address1", "address2", "city", "province",
                        "province_code", "zip", "country", "country_code",
                        "phone", "name",
                    ) and v
                }

            if customer_id:
                clean_cid = customer_id
                if isinstance(clean_cid, str) and "gid://shopify/Customer/" in clean_cid:
                    clean_cid = int(clean_cid.replace("gid://shopify/Customer/", ""))
                order_payload["customer"] = {"id": clean_cid}
            else:
                effective_email = email_override or original_order_data.get("email")
                if effective_email:
                    order_payload["email"] = effective_email
                phone = (
                    phone_override
                    or shipping_address.get("phone")
                    or original_order_data.get("phone")
                    or (state.get("phone_number", "") if state else "")
                )
                if phone:
                    order_payload["phone"] = phone

            # Order-level email/phone are independent of the customer
            # binding above (Shopify uses them for receipt + SMS even when
            # a customer_id is set), so apply overrides regardless.
            if email_override:
                order_payload["email"] = email_override
            if phone_override and "phone" not in order_payload:
                order_payload["phone"] = phone_override

            if note:
                order_payload["note"] = note
            elif original_order_data.get("note"):
                order_payload["note"] = original_order_data["note"]

            note_attrs = original_order_data.get("note_attributes")
            if note_attrs:
                order_payload["note_attributes"] = note_attrs

            discount_codes = original_order_data.get("discount_codes")
            if discount_codes:
                sanitized_discounts = []
                for dc in discount_codes:
                    if not dc.get("code"):
                        continue
                    dc_type = dc.get("type", "fixed_amount")
                    dc_amount = dc.get("amount", "0")
                    if dc_type == "percentage":
                        try:
                            abs_amount = float(dc_amount)
                        except (ValueError, TypeError):
                            abs_amount = 0.0
                        if abs_amount > 100:
                            subtotal = float(
                                original_order_data.get("total_line_items_price")
                                or original_order_data.get("subtotal_price")
                                or 0
                            )
                            if subtotal > 0:
                                pct = round((abs_amount / subtotal) * 100, 2)
                                pct = min(pct, 100.0)
                                dc_amount = str(pct)
                                log_with_trace_id(
                                    state,
                                    f"🔧 Discount '{dc.get('code')}': converted absolute amount "
                                    f"₹{abs_amount} to {pct}% (subtotal ₹{subtotal})",
                                )
                            else:
                                dc_type = "fixed_amount"
                                log_with_trace_id(
                                    state,
                                    f"🔧 Discount '{dc.get('code')}': cannot compute percentage "
                                    f"(subtotal=0), switching to fixed_amount ₹{abs_amount}",
                                    "warning",
                                )
                    sanitized_discounts.append(
                        {"code": dc["code"], "amount": dc_amount, "type": dc_type}
                    )
                if sanitized_discounts:
                    order_payload["discount_codes"] = sanitized_discounts

            # Carry over shipping lines (e.g. "Paid Shipping ₹100") so the
            # clone's total matches the original. Shopify defaults to zero
            # shipping when no shipping_lines are provided.
            original_shipping = original_order_data.get("shipping_lines") or []
            if original_shipping:
                order_payload["shipping_lines"] = [
                    {
                        k: v for k, v in sl.items()
                        if k in ("title", "code", "price", "source", "carrier_identifier")
                        and v is not None
                    }
                    for sl in original_shipping
                    if not sl.get("is_removed")
                ]

            # Carry over tax lines so the clone's tax total matches the
            # original. Without this Shopify may auto-calculate differently
            # or default to zero tax.
            original_tax_lines = original_order_data.get("tax_lines") or []
            if original_tax_lines:
                order_payload["tax_lines"] = [
                    {k: v for k, v in tl.items() if k in ("title", "price", "rate") and v is not None}
                    for tl in original_tax_lines
                ]

            if original_order_data.get("tax_exempt"):
                order_payload["tax_exempt"] = True
            if original_order_data.get("taxes_included"):
                order_payload["taxes_included"] = True

            if transactions:
                order_payload["transactions"] = transactions

            from fashion_bot.utils.http_client import get_shared_async_http_client
            shop_domain = shop_url.replace("https://", "").replace("http://", "").rstrip("/")
            url = f"https://{shop_domain}/admin/api/{api_version}/orders.json"
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            }

            log_with_trace_id(
                state,
                f"📤 Clone order POST to {url} with {len(order_payload.get('line_items', []))} line items, "
                f"financial_status={order_payload.get('financial_status')}",
            )
            client = await get_shared_async_http_client()
            resp = await client.post(url, json={"order": order_payload}, headers=headers, timeout=15)

            if resp.status_code == 201:
                order = resp.json().get("order", {})
                order_name = order.get("name", "N/A")
                log_with_trace_id(state, f"✅ Cloned order created: {order_name}")
                return {
                    "success": True,
                    "order": order,
                    "order_id": order_name,
                    "order_name": order_name,
                    "order_number": str(order.get("order_number", "")),
                    "total_price": order.get("total_price", ""),
                    "financial_status": order.get("financial_status", ""),
                }

            try:
                error_body = resp.json()
            except Exception:
                error_body = resp.text[:500]
            log_with_trace_id(state, f"❌ Clone order Shopify error {resp.status_code}: {error_body}", "error")
            return {"success": False, "error": f"Shopify HTTP {resp.status_code}", "details": error_body}

        except Exception as exc:
            error_msg = str(exc) or f"{type(exc).__name__} (no message)"
            log_with_trace_id(state, f"❌ Exception in aclone_order: {error_msg}", "error")
            return {"success": False, "error": error_msg}

    @staticmethod
    def _extract_handle_from_url(url: str) -> Optional[str]:
        match = re.search(r"/products/([^/?#]+)", url)
        if match:
            return match.group(1)
        if url and not url.startswith("http"):
            return url
        return None

    @staticmethod
    def _variant_size_label(variant: Dict[str, Any]) -> str:
        """Human-readable size label for a variant (the 'Size' option, else title)."""
        for option in variant.get("selectedOptions", []):
            if option.get("name", "").lower() == "size":
                return option.get("value") or ""
        return variant.get("title") or ""

    @staticmethod
    def _find_variant_by_size(
        product_data: Dict[str, Any],
        requested_size: str,
        state: Optional[Dict] = None,
        requested_variant_id: str = "",
    ) -> Dict[str, Any]:
        """Resolve the variant to order, by exact id when known, else by size label.

        ``requested_variant_id`` is the variant the customer already committed to
        (a cart line, or a variant read out of surfaced product context). It is
        preferred over ``requested_size`` because it is unambiguous, whereas the
        size label is retyped by the agent and has to survive store-data quirks
        (stray double spaces, punctuation) to match.
        """
        variants = product_data.get("variants", [])
        if not variants:
            log_with_trace_id(
                state,
                f"❌ VARIANT RESOLUTION FAILED: product '{product_data.get('title', '')}' "
                f"has no variants. Order cannot be created.",
                "error",
            )
            return {"success": False, "error": "No variants found for this product"}

        def _resolve(variant: Dict[str, Any]) -> Dict[str, Any]:
            # Stock gate: never resolve a size the customer can't actually buy.
            # Shopify's REST order-create endpoint does NOT enforce inventory, so
            # this is the gate that stops out-of-stock COD orders being created.
            if not variant_is_available(variant):
                in_stock = [
                    label for v in variants
                    if variant_is_available(v)
                    and (label := ShopifyOrderAdapter._variant_size_label(v))
                ]
                msg = f"Size '{requested_size}' is out of stock."
                if in_stock:
                    msg += f" In-stock sizes: {', '.join(in_stock)}."
                else:
                    msg += " This product is currently out of stock in all sizes."
                log_with_trace_id(
                    state,
                    f"🚫 STOCK GUARD: requested size '{requested_size}' resolved to out-of-stock "
                    f"variant {variant.get('id')} ({variant.get('title')}). Blocking order.",
                    "warning",
                )
                return {
                    "success": False,
                    "out_of_stock": True,
                    "error": msg,
                    "message": msg,
                    "requested_size": requested_size,
                    "in_stock_sizes": in_stock,
                }
            variant_id = str(variant.get("id", "")).replace("gid://shopify/ProductVariant/", "")
            return {"success": True, "variant_id": variant_id, "variant_title": variant.get("title")}

        # Exact variant the customer already committed to. Checked first so a cart
        # checkout never re-derives a variant the cart had already pinned down.
        if requested_variant_id:
            by_id = find_variant_by_id(variants, requested_variant_id)
            if by_id is not None:
                # Both supplied and disagreeing means the agent's id and its size
                # label came from different items. The id wins (the customer picked
                # it in the cart; the label is transcription), but say so loudly —
                # silent disagreement here would ship the wrong item.
                if requested_size:
                    by_size = resolve_variant_match(variants, requested_size)
                    if by_size is not None and by_size.get("id") != by_id.get("id"):
                        log_with_trace_id(
                            state,
                            f"⚠️ variant_id {requested_variant_id} resolves to "
                            f"{by_id.get('title')!r} but size {requested_size!r} resolves to "
                            f"{by_size.get('title')!r} — ordering by id.",
                            "warning",
                        )
                return _resolve(by_id)
            log_with_trace_id(
                state,
                f"⚠️ variant_id '{requested_variant_id}' is not a variant of "
                f"'{product_data.get('title', '')}' — falling back to size matching.",
                "warning",
            )

        # Shared matcher: handles single-option (size-only / colour-only) and
        # multi-option "Colour / Size" compound-title products identically to the
        # in-place order-edit path.
        matched = resolve_variant_match(variants, requested_size)
        if matched is not None:
            return _resolve(matched)

        if len(variants) == 1:
            log_with_trace_id(
                state,
                f"⚠️ Only one variant exists, using default: {variants[0].get('title')}",
            )
            return _resolve(variants[0])

        available_sizes = []
        for variant in variants:
            for option in variant.get("selectedOptions", []):
                if option.get("name", "").lower() == "size":
                    available_sizes.append(option.get("value"))

        if not available_sizes:
            available_sizes = [v.get("title", "Unknown") for v in variants]

        # Logged at error level so a dropped order is visible in Grafana. Without
        # this the failure is only a returned dict — the whole turn looks healthy
        # in the logs while the customer is told to go check out on the website.
        log_with_trace_id(
            state,
            f"❌ VARIANT RESOLUTION FAILED: requested size {requested_size!r} matched no "
            f"variant of '{product_data.get('title', '')}'. Available: "
            f"{[repr(s) for s in available_sizes]}. Order cannot be created.",
            "error",
        )
        return {
            "success": False,
            "error": f"Size '{requested_size}' not found. Available sizes: {', '.join(available_sizes)}",
        }

    @staticmethod
    def _parse_address(address: str) -> Dict[str, str]:
        parts = [part.strip() for part in address.split(",") if part.strip()]
        parsed = {
            "address1": "",
            "address2": "",
            "city": "",
            "state": "",
            "zip_code": "",
            "country": "India",
        }

        if not parts:
            return parsed

        zip_match = re.search(r"\b(\d{6})\b", address)
        if zip_match:
            parsed["zip_code"] = zip_match.group(1)
            # Strip pincode-only segments so the positional city/state
            # heuristic below doesn't consume the pincode as a slot.
            pin = zip_match.group(1)
            parts = [p for p in parts if p.strip().strip(".") != pin]

        if not parts:
            return parsed

        parsed["address1"] = parts[0]
        if len(parts) >= 2:
            parsed["city"] = parts[-2]
            parsed["state"] = parts[-1]
        if len(parts) >= 3:
            parsed["address2"] = ", ".join(parts[1:-2])
        return parsed

    def cancel_order(
        self,
        order_id: str,
        reason: str = "",
        state: Optional[Dict] = None,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        self._raise_sync_unavailable("cancel_order")

    async def acancel_order(
        self,
        order_id: str,
        reason: str = "",
        state: Optional[Dict] = None,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        try:
            config = await self._aget_config(state)
            access_token = config.get("access_token")
            shop_url = config.get("shop_url")
            api_version = config.get("api_version", "2024-04")
            if not access_token or not shop_url:
                return {"success": False, "error": self._missing_integration_error(), "order_id": order_id}

            order = await self.aget_order_details(order_id, state=state)
            if not order:
                return {"success": False, "error": f"Order {order_id} not found", "order_id": order_id}

            if order.get("cancelled_at"):
                return {
                    "success": False,
                    "error": f"Order {order_id} is already cancelled",
                    "order_id": order_id,
                }
            if (order.get("fulfillment_status") or "").lower() == "fulfilled":
                return {
                    "success": False,
                    "error": f"Order {order_id} has already been shipped/fulfilled and cannot be cancelled.",
                    "order_id": order_id,
                }

            reason_mapping = {
                "wrong_size": "customer",
                "ordered_by_mistake": "customer",
                "delivery_too_slow": "customer",
                "found_better_price": "customer",
                "changed_mind": "customer",
                "out_of_stock": "inventory",
                "fraud": "fraud",
                "payment_declined": "declined",
            }
            api_reason = reason_mapping.get((reason or "").lower(), "other")
            payload = {
                "reason": api_reason,
                "email": True,
                "restock": True,
                "currency": order.get("currency", "INR"),
            }
            financial_status = (order.get("financial_status") or "").lower()
            if financial_status in ("paid", "partially_paid") and not skip_refund:
                try:
                    refund_amount = float(order.get("total_price", "0")) - float(order.get("total_outstanding", "0"))
                    if refund_amount > 0:
                        payload["amount"] = str(refund_amount)
                except (TypeError, ValueError):
                    pass

            # Add cancellation note (auto-generate from reason if not provided)
            if custom_note:
                note_text = f"[Bloomerce] Order cancelled by customer request. Reason: {custom_note}"
            else:
                note_text = f"[Bloomerce] Order cancelled by customer request. Reason: {reason}"
            note_result = await self.aadd_order_note(order_id, note_text, state=state)
            if not note_result.get("success"):
                return note_result

            # Cancel in the client's configured logistics partner first (always
            # proceed to Shopify even if this fails). Resolve the partner via the
            # factory instead of hard-coding Shiprocket, so Delhivery-only (and
            # other) tenants cancel against their actual partner rather than
            # erroring with a misleading "Shiprocket configuration missing".
            logistics_result = None
            try:
                from fashion_bot.core.factory import ServiceFactory
                client_id = self._get_client_id(state)
                logistics_adapter = await ServiceFactory.aget_logistics_service(client_id=client_id, state=state)
                logistics_result = await logistics_adapter.acancel_shipment(order_id, state=state)
            except Exception as sr_exc:
                logger.warning("Logistics cancellation failed for %s: %s", order_id, sr_exc)

            client = await get_shared_async_http_client()
            cancel_url = f"https://{shop_url}/admin/api/{api_version}/orders/{order['id']}/cancel.json"
            _t0 = time.monotonic()
            response = await client.post(
                cancel_url,
                json=payload,
                headers=self._get_headers(access_token),
                timeout=30,
            )
            logger.info(f"[SHOPIFY] POST order_cancel elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
            if response.status_code == 200:
                result = {
                    "success": True,
                    "order_id": order_id,
                    "message": f"Order {order_id} has been successfully cancelled",
                    "cancellation_reason": reason,
                }
                if logistics_result:
                    result["logistics"] = logistics_result

                if financial_status in ("paid", "partially_paid") and not skip_refund:
                    try:
                        refund_result = await self.arefund_order(order_id, state=state)
                        result["refund"] = refund_result
                        if refund_result.get("success"):
                            result["message"] += " and refund processed"
                        else:
                            log_with_trace_id(state, f"⚠️ Refund failed for {order_id}: {refund_result.get('error')}", "warning")
                    except Exception as refund_exc:
                        log_with_trace_id(state, f"⚠️ Refund attempt failed for {order_id}: {refund_exc}", "warning")
                        result["refund"] = {"success": False, "error": str(refund_exc)}

                return result

            try:
                error_details = response.json()
            except ValueError:
                error_details = response.text
            return {
                "success": False,
                "order_id": order_id,
                "error": f"Shopify API error: {response.status_code}",
                "details": error_details,
            }
        except Exception as exc:
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def update_order(self, order_id: str, update_data: Dict[str, Any], state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("update_order")

    async def aupdate_order(
        self,
        order_id: str,
        update_data: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShopifyAdapter: Async updating order {order_id}")

        try:
            config = await self._aget_config(state)
            access_token = config.get("access_token")
            shop_url = config.get("shop_url")
            if not access_token or not shop_url:
                return {"success": False, "error": self._missing_integration_error(), "order_id": order_id}

            existing_order = await self.aget_order_details(order_id, state=state)
            if not existing_order:
                return {"success": False, "error": "Order not found", "order_id": order_id}

            update_type = update_data.get("update_type", "multiple")
            new_address = update_data.get("new_address")
            delivery_instructions = update_data.get("delivery_instructions")
            new_phone = update_data.get("new_phone")

            existing_shipping = existing_order.get("shipping_address", {}) or {}
            existing_first_name = existing_shipping.get("first_name", "")
            existing_last_name = existing_shipping.get("last_name", "")
            existing_phone = existing_shipping.get("phone", "")

            shipping_address = None
            address_warnings: List[str] = []
            note_parts: List[str] = []

            if update_type == "name":
                new_first = update_data.get("first_name", existing_first_name or "")
                new_last = update_data.get("last_name", existing_last_name or "") or "."
                addr_patch: Dict[str, str] = {
                    "first_name": new_first,
                    "last_name": new_last,
                }
                addr_patch.update(self._heal_shipping_address(
                    existing_shipping, state=state, order_id=order_id,
                ))
                rest_payload = {
                    "order": {
                        "id": existing_order["id"],
                        "shipping_address": addr_patch,
                    }
                }
                try:
                    response = await self._aorder_put(
                        numeric_order_id=str(existing_order["id"]),
                        payload=rest_payload,
                        config=config,
                    )
                    resp_data = response.json()
                    if response.status_code == 200 and resp_data.get("order"):
                        note_text = f"[Bloomerce] Customer name updated to: {new_first} {new_last}"
                        await self.aadd_order_note(order_id, note_text, state=state)
                        return {
                            "success": True,
                            "order_id": order_id,
                            "message": f"Name updated to {new_first} {new_last}",
                            "updated": ["name"],
                        }
                    errors = resp_data.get("errors", "Unknown error")
                    return {
                        "success": False,
                        "order_id": order_id,
                        "error": f"Shopify order update failed: {errors}",
                    }
                except Exception as exc:
                    log_with_trace_id(state, f"⚠️ Shopify REST name update failed for {order_id}: {exc}", "warning")
                    return {"success": False, "error": str(exc), "order_id": order_id}

            if update_type in ["address", "multiple"] and new_address:
                address_parts = new_address.split("|")
                if len(address_parts) < 6:
                    return {"success": False, "error": "Invalid address format", "order_id": order_id}

                from fashion_bot.shopify.modules.order_updation_api import ShippingAddress

                names = address_parts[0].split(" ", 1)
                llm_first_name = names[0] if names else ""
                llm_last_name = names[1] if len(names) > 1 else ""
                first_name = existing_first_name or llm_first_name
                last_name = existing_last_name or llm_last_name or "."
                phone = address_parts[6] if len(address_parts) > 6 and address_parts[6] else (new_phone or existing_phone or "")

                address_model = ShippingAddress(
                    first_name=first_name,
                    last_name=last_name,
                    address1=address_parts[1],
                    address2=address_parts[2] if len(address_parts) > 2 else "",
                    city=address_parts[3] if len(address_parts) > 3 else "",
                    state=address_parts[4] if len(address_parts) > 4 else "",
                    zip_code=address_parts[5] if len(address_parts) > 5 else "",
                    phone=phone,
                    country=existing_shipping.get("country", "India"),
                )
                address_validation = await address_model.avalidate()
                if not address_validation.get("valid"):
                    return {
                        "success": False,
                        "error": "Validation failed",
                        "order_id": order_id,
                        "details": address_validation.get("errors", []),
                        "warnings": address_validation.get("warnings", []),
                    }
                address_warnings = address_validation.get("warnings", [])

                shipping_address = {
                    "firstName": address_model.first_name,
                    "lastName": address_model.last_name,
                    "address1": address_model.address1,
                    "address2": address_model.address2,
                    "city": address_model.city,
                    "province": address_model.state,
                    "zip": address_model.zip_code,
                    "country": address_model.country,
                    "phone": address_model.phone,
                }

            if update_type in ["instructions", "multiple"] and delivery_instructions:
                note_parts.append(f"Delivery Instructions: {delivery_instructions}")

            if update_type in ["phone", "multiple"] and new_phone and not shipping_address:
                note_parts.append(f"Updated Phone: {new_phone}")

            if shipping_address:
                graphql_result = await self._aorder_update_graphql(
                    numeric_order_id=str(existing_order["id"]),
                    config=config,
                    shipping_address=shipping_address,
                    note="\n".join(note_parts) if note_parts else None,
                )
                if not graphql_result.get("success"):
                    return {
                        "success": False,
                        "order_id": order_id,
                        "error": graphql_result.get("error", "Unknown error"),
                        "details": graphql_result.get("details", []),
                    }

            elif note_parts:
                note_result = await self.aadd_order_note(order_id, "\n".join(note_parts), state=state)
                if not note_result.get("success"):
                    return note_result

            updated_items = []
            if shipping_address:
                updated_items.append("shipping address")
            if delivery_instructions:
                updated_items.append("delivery instructions")
            if new_phone and not shipping_address:
                updated_items.append("contact phone")
            if update_type == "name":
                updated_items = ["customer name"]

            return {
                "success": True,
                "order_id": order_id,
                "updated": updated_items,
                "message": "Order updated successfully",
                "warnings": address_warnings,
            }
        except Exception as exc:
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def filter_delivered_orders(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter orders to only include those with confirmed DELIVERED status.
        fulfilled != delivered. fulfilled means shipped/dispatched.
        Only include orders where logistics data confirms actual delivery.
        Checks Shopify fulfillments[].shipment_status and Shiprocket enrichment.
        """
        delivered_statuses = {"delivered", "rto delivered"}
        result = []
        for order in orders:
            if order.get("delivered_date"):
                result.append(order)
                continue
            if (order.get("partner_status") or "").lower() in delivered_statuses:
                result.append(order)
                continue
            if (order.get("shipment_status") or "").lower() in delivered_statuses:
                result.append(order)
                continue
            fulfillments = order.get("fulfillments") or []
            for f in fulfillments:
                if (f.get("shipment_status") or "").lower() in delivered_statuses:
                    result.append(order)
                    break
        return result

    def format_delivered_orders_for_display(self, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Format delivered orders for display to the user.
        Uses Shopify-specific field names.
        """
        formatted_orders = []
        for order in orders:
            formatted_order = {
                "order_id": order.get("id"),
                "order_name": order.get("name"),  # Shopify uses 'name' like #1001
                "channel_order_id": order.get("order_number"),
                "status": order.get("partner_status") or order.get("fulfillment_status"),
                "delivered_date": order.get("closed_at") or order.get("updated_at"),
                "created_at": order.get("created_at"),
                "items": []
            }

            # Extract line items - Shopify uses 'line_items'
            line_items = order.get("line_items", [])
            for item in line_items:
                formatted_order["items"].append({
                    "name": item.get("name") or item.get("title"),
                    "quantity": item.get("quantity", 1),
                    "sku": item.get("sku"),
                    "variant_id": item.get("variant_id")
                })

            formatted_orders.append(formatted_order)

        return formatted_orders

    def build_delivered_orders_response(self, orders: List[Dict[str, Any]], phone_number: str) -> Dict[str, Any]:
        """
        Build a complete response for delivered orders query.
        """
        delivered_orders = self.filter_delivered_orders(orders)
        formatted_orders = self.format_delivered_orders_for_display(delivered_orders)

        return {
            "success": True,
            "phone_number": phone_number,
            "total_orders": len(orders),
            "delivered_orders_count": len(delivered_orders),
            "delivered_orders": formatted_orders,
            "message": f"Found {len(delivered_orders)} delivered order(s) eligible for return/exchange."
            if delivered_orders else "No delivered orders found for this customer."
        }

    def get_customer_by_phone(self, phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_customer_by_phone")

    async def aget_customer_by_phone(self, phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        from fashion_bot.shopify.modules.customer_apis import afetch_customer_data_from_shopify

        log_with_trace_id(state, "ShopifyAdapter: Async fetching customer by phone")
        try:
            config = await self._aget_config(state)
            if not config.get("access_token") or not config.get("shop_url"):
                return {"found": False, "error": "Configuration not available"}

            customer_data = await afetch_customer_data_from_shopify(phone, config)
            if customer_data.get("found"):
                return {
                    "found": True,
                    "is_returning_customer": True,
                    "customer_name": customer_data.get("customer_name", ""),
                    "customer_address": customer_data.get("customer_address", ""),
                    "customer_email": customer_data.get("email", ""),
                    "message": "Returning customer found",
                }
            return {
                "found": False,
                "is_returning_customer": False,
                "message": "New customer - will need to collect name and address",
            }
        except Exception as exc:
            log_with_trace_id(state, f"Error fetching customer: {exc}", "error")
            return {"found": False, "error": str(exc)}

    # Shopify caps the order ``note`` field at ~5000 characters. Our note-add
    # flow appends to the existing note on every call, so a heavily-updated
    # order eventually crosses that cap and Shopify rejects the PUT with a
    # 422. When that happens we drop the oldest ~50% of the existing note and
    # retry once — see ``_trim_note_front`` and ``aadd_order_note``.
    _NOTE_TRIM_FRACTION = 0.5

    @staticmethod
    def _trim_note_front(existing_note: str, fraction: float = _NOTE_TRIM_FRACTION) -> str:
        """Drop roughly the first ``fraction`` of ``existing_note`` from the top.

        The cut point is snapped forward to the next newline so only whole
        lines are removed — an existing sentence is never truncated mid-way.
        Newlines act as the separator. Returns the surviving tail with any
        leading blank lines stripped.
        """
        if not existing_note:
            return ""
        cut = int(len(existing_note) * fraction)
        # Snap to the next line boundary at/after the cut so whole lines
        # (and thus whole sentences) are preserved.
        newline_idx = existing_note.find("\n", cut)
        if newline_idx == -1:
            # No newline past the cut point — fall back to the previous one so
            # we still cut on a line boundary rather than mid-sentence.
            newline_idx = existing_note.rfind("\n", 0, cut)
        if newline_idx == -1:
            # Note has no newlines at all; hard-cut at the fraction as a last
            # resort (nothing better available to preserve sentence bounds).
            trimmed = existing_note[cut:]
        else:
            trimmed = existing_note[newline_idx + 1:]
        return trimmed.lstrip("\n")

    def add_order_note(self, order_id: str, note: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("add_order_note")

    async def aadd_order_note(
        self,
        order_id: str,
        note: str,
        state: Optional[Dict] = None,
        order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShopifyAdapter: Async adding note to order {order_id}")
        try:
            config = await self._aget_config(state)
            if not config.get("access_token") or not config.get("shop_url"):
                return {"success": False, "error": "Configuration not available", "order_id": order_id}

            # Reuse a caller-supplied record (already fetched upstream) to avoid a
            # redundant lookup; only fetch when no usable record was passed.
            if isinstance(order_record, dict) and order_record.get("id"):
                order = order_record
            else:
                order = await self.aget_order_details(order_id, state=state)
            if not order:
                return {"success": False, "error": "Order not found", "order_id": order_id}

            existing_note = order.get("note", "")
            combined_note = f"{existing_note}\n\n{note}" if existing_note else note
            try:
                response = await self._aorder_put(
                    numeric_order_id=str(order["id"]),
                    payload={"order": {"id": order["id"], "note": combined_note}},
                    config=config,
                )
            except httpx.HTTPStatusError as http_exc:
                # A note-only PUT that 422s is Shopify rejecting an over-long
                # ``note`` (its field caps at ~5000 chars). Drop the oldest
                # ~50% of the existing note — snapped to a line boundary so no
                # sentence is cut mid-way — re-append the new note, and retry
                # once. Any other status, or nothing to trim, re-raises.
                if http_exc.response.status_code != 422 or not existing_note:
                    raise
                trimmed_existing = self._trim_note_front(existing_note)
                log_with_trace_id(
                    state,
                    f"Note add 422 for order {order_id}: note length "
                    f"{len(combined_note)} exceeded Shopify's limit; trimmed "
                    f"existing note {len(existing_note)}->{len(trimmed_existing)} "
                    f"chars and retrying",
                    "warning",
                )
                combined_note = f"{trimmed_existing}\n\n{note}" if trimmed_existing else note
                response = await self._aorder_put(
                    numeric_order_id=str(order["id"]),
                    payload={"order": {"id": order["id"], "note": combined_note}},
                    config=config,
                )
            return {
                "success": response.status_code == 200,
                "order_id": order_id,
                "note_added": note,
            }
        except Exception as exc:
            log_with_trace_id(state, f"Error adding note: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def add_order_tags(self, order_id: str, tags: List[str], state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("add_order_tags")

    async def aadd_order_tags(
        self,
        order_id: str,
        tags: List[str],
        state: Optional[Dict] = None,
        order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShopifyAdapter: Async adding tags to order {order_id}")
        try:
            config = await self._aget_config(state)
            if not config.get("access_token") or not config.get("shop_url"):
                return {"success": False, "error": "Configuration not available", "order_id": order_id}

            # Reuse a caller-supplied record (already fetched upstream) to avoid a
            # redundant lookup; only fetch when no usable record was passed.
            if isinstance(order_record, dict) and order_record.get("id"):
                order = order_record
            else:
                order = await self.aget_order_details(order_id, state=state)
            if not order:
                return {"success": False, "error": "Order not found", "order_id": order_id}

            existing_tags = [tag.strip() for tag in (order.get("tags") or "").split(",") if tag.strip()]
            merged_tags = existing_tags[:]
            for tag in tags:
                if tag not in merged_tags:
                    merged_tags.append(tag)

            response = await self._aorder_put(
                numeric_order_id=str(order["id"]),
                payload={"order": {"id": order["id"], "tags": ", ".join(merged_tags)}},
                config=config,
            )
            return {
                "success": response.status_code == 200,
                "order_id": order_id,
                "tags_added": tags,
            }
        except Exception as exc:
            log_with_trace_id(state, f"Error adding tags: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def update_order_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("update_order_phone")

    async def aupdate_order_phone(self, order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShopifyAdapter: Async updating phone for order {order_id}")
        try:
            config = await self._aget_config(state)
            if not config.get("access_token") or not config.get("shop_url"):
                return {"success": False, "error": "Configuration not available", "order_id": order_id}

            order = await self.aget_order_details(order_id, state=state)
            if not order:
                return {"success": False, "error": "Order not found", "order_id": order_id}

            cleaned_phone = self._normalize_phone_value(new_phone)
            existing_shipping = order.get("shipping_address") or {}
            addr_patch: Dict[str, str] = {"phone": cleaned_phone}
            addr_patch.update(self._heal_shipping_address(
                existing_shipping, state=state, order_id=order_id,
            ))
            response = await self._aorder_put(
                numeric_order_id=str(order["id"]),
                payload={
                    "order": {
                        "id": order["id"],
                        "phone": cleaned_phone,
                        "shipping_address": addr_patch,
                    }
                },
                config=config,
            )
            resp_data = response.json()
            if response.status_code == 200 and resp_data.get("order"):
                return {"success": True, "order_id": order_id, "new_phone": new_phone, "message": "Phone updated on Shopify"}
            errors = resp_data.get("errors", "Unknown error")
            return {
                "success": False,
                "order_id": order_id,
                "error": f"Phone update failed: {errors}",
            }
        except Exception as exc:
            log_with_trace_id(state, f"Error updating phone: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def refund_order(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("refund_order")

    async def arefund_order(self, order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Issue a full refund for a cancelled/paid order via Shopify's Refund API.

        Flow: resolve order -> find capture transaction -> calculate refund -> create refund.
        """
        try:
            config = await self._aget_config(state)
            access_token = config.get("access_token")
            shop_url = config.get("shop_url")
            api_version = config.get("api_version", "2024-04")
            if not access_token or not shop_url:
                return {"success": False, "error": self._missing_integration_error(), "order_id": order_id}

            order = await self.aget_order_details(order_id, state=state)
            if not order:
                return {"success": False, "error": f"Order {order_id} not found", "order_id": order_id}

            numeric_id = order["id"]
            financial_status = (order.get("financial_status") or "").lower()
            if financial_status in ("refunded", "voided"):
                return {"success": True, "order_id": order_id, "message": f"Order already {financial_status}", "already_refunded": True}

            if financial_status not in ("paid", "partially_paid"):
                return {"success": False, "order_id": order_id, "error": f"Order financial_status is '{financial_status}', nothing to refund"}

            client = await get_shared_async_http_client()
            headers = self._get_headers(access_token)
            base = f"https://{shop_url}/admin/api/{api_version}"

            # 1. Get transactions to find the parent capture/sale
            txn_url = f"{base}/orders/{numeric_id}/transactions.json"
            _t0 = time.monotonic()
            txn_resp = await client.get(txn_url, headers=headers, timeout=30)
            logger.info(f"[SHOPIFY] GET order_transactions elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={txn_resp.status_code}")
            self._raise_for_shopify_response(txn_resp)

            transactions = txn_resp.json().get("transactions", [])
            parent_txn = None
            for txn in transactions:
                if txn.get("kind") in ("capture", "sale") and txn.get("status") == "success":
                    parent_txn = txn
                    break

            # 2. Calculate the refund
            line_items_payload = []
            for item in order.get("line_items", []):
                line_items_payload.append({
                    "line_item_id": item["id"],
                    "quantity": item.get("quantity", 1),
                })

            calc_payload = {
                "refund": {
                    "shipping": {"full_refund": True},
                    "refund_line_items": line_items_payload,
                }
            }
            calc_url = f"{base}/orders/{numeric_id}/refunds/calculate.json"
            _t0 = time.monotonic()
            calc_resp = await client.post(calc_url, json=calc_payload, headers=headers, timeout=30)
            logger.info(f"[SHOPIFY] POST refund_calculate elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={calc_resp.status_code}")
            self._raise_for_shopify_response(calc_resp)
            calculated = calc_resp.json().get("refund", {})

            # 3. Build refund transactions
            # Use the parent transaction's amount (what was actually captured) rather
            # than total_price, so partially_paid orders only refund the captured amount.
            refund_amount = float(parent_txn["amount"]) if parent_txn else float(order.get("total_price", "0"))
            refund_transactions = calculated.get("transactions", [])
            if parent_txn:
                if not refund_transactions:
                    refund_transactions = [{
                        "parent_id": parent_txn["id"],
                        "amount": str(refund_amount),
                        "kind": "refund",
                        "gateway": parent_txn.get("gateway", ""),
                    }]
                else:
                    for rt in refund_transactions:
                        rt["parent_id"] = parent_txn["id"]
                        rt["kind"] = "refund"
                        if not rt.get("gateway"):
                            rt["gateway"] = parent_txn.get("gateway", "")
            else:
                # Admin API / manually-paid orders may have no capture transaction.
                # Create a line-item-only refund (no monetary transaction). Shopify
                # won't change financial_status but the items are marked refunded.
                # Real orders with actual payment gateways always have a parent txn.
                log_with_trace_id(state, f"⚠️ No parent transaction for {order_id} — creating line-item-only refund", "warning")
                refund_transactions = []

            create_payload = {
                "refund": {
                    "notify": True,
                    "shipping": calculated.get("shipping", {"full_refund": True}),
                    "refund_line_items": calculated.get("refund_line_items", line_items_payload),
                    "transactions": refund_transactions,
                }
            }
            create_url = f"{base}/orders/{numeric_id}/refunds.json"
            _t0 = time.monotonic()
            create_resp = await client.post(create_url, json=create_payload, headers=headers, timeout=30)
            logger.info(f"[SHOPIFY] POST refund_create elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={create_resp.status_code}")

            if create_resp.status_code in (200, 201):
                refund_data = create_resp.json().get("refund", {})
                return {
                    "success": True,
                    "order_id": order_id,
                    "refund_id": refund_data.get("id"),
                    "message": f"Refund processed for order {order_id}",
                }

            try:
                error_details = create_resp.json()
            except ValueError:
                error_details = create_resp.text
            return {
                "success": False,
                "order_id": order_id,
                "error": f"Refund API error: {create_resp.status_code}",
                "details": error_details,
            }
        except Exception as exc:
            log_with_trace_id(state, f"Error refunding order {order_id}: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    def update_order_email(self, order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("update_order_email")

    async def aupdate_order_email(self, order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShopifyAdapter: Async updating email for order {order_id}")
        try:
            config = await self._aget_config(state)
            if not config.get("access_token") or not config.get("shop_url"):
                return {"success": False, "error": "Configuration not available", "order_id": order_id}

            email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
            if not re.match(email_pattern, new_email):
                return {"success": False, "error": "Invalid email format", "order_id": order_id}

            order = await self.aget_order_details(order_id, state=state)
            if not order:
                return {"success": False, "error": "Order not found", "order_id": order_id}

            response = await self._aorder_put(
                numeric_order_id=str(order["id"]),
                payload={
                    "order": {
                        "id": order["id"],
                        "email": new_email,
                        "contact_email": new_email,
                    }
                },
                config=config,
            )
            if response.status_code == 200:
                return {"success": True, "order_id": order_id, "new_email": new_email, "message": "Email updated"}
            return {"success": False, "error": f"API error: {response.status_code}", "order_id": order_id}
        except Exception as exc:
            log_with_trace_id(state, f"Error updating email: {exc}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}
