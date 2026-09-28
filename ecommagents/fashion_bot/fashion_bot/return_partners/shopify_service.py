"""Shopify-native implementation of the generic return partner interface."""

from __future__ import annotations

from typing import Any

from fashion_bot.return_partners.registry import register_return_partner


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


class ShopifyReturnPartnerService:
    partner_name = "shopify"

    def _normalize_order_name(self, order_number: str) -> str:
        cleaned = str(order_number or "").strip()
        return cleaned if cleaned.startswith("#") else f"#{cleaned}" if cleaned else ""

    async def _aget_order(self, client_id: str, order_number: str, state: dict | None = None) -> dict:
        from fashion_bot.core.factory import ServiceFactory

        order_service = await ServiceFactory.aget_order_service(
            client_id=client_id,
            state=state,
            vendor="shopify",
        )
        return await order_service.aget_order_details(order_number, state=state) or {}

    async def _aget_shopify_native_config(self, client_id: str) -> dict:
        try:
            from fashion_bot.config_manager import aget_json_config

            rules = await aget_json_config("return_exchange_rules", client_id=client_id) or {}
        except Exception:
            rules = {}
        native = rules.get("shopify_native") if isinstance(rules.get("shopify_native"), dict) else {}
        return native

    def _normalize_return(self, item: dict, order_name: str | None = None) -> dict:
        line_items = []
        for node in ((item.get("returnLineItems") or {}).get("nodes") or []):
            fulfillment_line = node.get("fulfillmentLineItem") or {}
            line = fulfillment_line.get("lineItem") or {}
            product = line.get("product") or {}
            variant = line.get("variant") or {}
            line_items.append(
                {
                    "original_product": _first_non_empty(line.get("name"), product.get("title")),
                    "original_variant": variant.get("title"),
                    "quantity": node.get("quantity"),
                    "reason": node.get("returnReason"),
                    "customer_note": node.get("customerNote"),
                    "product_id": product.get("id"),
                    "variant_id": variant.get("id"),
                    "fulfillment_line_item_id": fulfillment_line.get("id"),
                }
            )
        exchange_line_items = []
        for node in ((item.get("exchangeLineItems") or {}).get("nodes") or []):
            exchange_lines = []
            for line in node.get("lineItems") or []:
                if not isinstance(line, dict):
                    continue
                product = line.get("product") or {}
                variant = line.get("variant") or {}
                exchange_lines.append(
                    {
                        "line_item_id": line.get("id"),
                        "title": line.get("name"),
                        "quantity": line.get("quantity"),
                        "product_id": product.get("id"),
                        "product_title": product.get("title"),
                        "variant_id": variant.get("id"),
                        "variant_title": variant.get("title"),
                    }
                )
            exchange_line_items.append(
                {
                    "id": node.get("id"),
                    "quantity": node.get("quantity"),
                    "variant_id": node.get("variantId"),
                    "line_items": exchange_lines,
                }
            )
        return {
            "request_id": item.get("id"),
            "request_number": item.get("name"),
            "request_type": "exchange" if exchange_line_items else "return",
            "status": item.get("status"),
            "order_name": order_name,
            "line_items": line_items,
            "exchange_line_items": exchange_line_items,
            "created_at": item.get("createdAt"),
            "updated_at": item.get("updatedAt"),
            "raw": item,
        }

    async def list_requests_by_order_number(
        self,
        client_id: str,
        order_number: str,
        *,
        request_type: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        order = await self._aget_order(client_id, order_number)
        order_gid = order.get("admin_graphql_api_id") or order.get("id")
        if not order_gid:
            return {
                "success": False,
                "message": f"Could not find Shopify order {order_number}.",
                "order_name": self._normalize_order_name(order_number),
            }
        from fashion_bot.shopify.modules.return_apis import aget_order_returns

        result = await aget_order_returns(client_id=client_id, order_gid_or_id=order_gid)
        if not result.get("success"):
            return {**result, "order_name": order.get("name") or order_number}
        order_payload = ((result.get("data") or {}).get("order") or {})
        requests = [
            self._normalize_return(item, order_payload.get("name") or order.get("name"))
            for item in (((order_payload.get("returns") or {}).get("nodes")) or [])
            if isinstance(item, dict)
        ]
        normalized_type = str(request_type or "").strip().lower()
        if normalized_type in {"return", "exchange"}:
            requests = [
                request
                for request in requests
                if request.get("request_type") == normalized_type
            ]
        return {
            "success": True,
            "status_code": 200,
            "order_name": order_payload.get("name") or order.get("name"),
            "requests": requests,
        }

    async def get_status_by_order_number(
        self,
        client_id: str,
        order_number: str,
        *,
        request_type: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        result = await self.list_requests_by_order_number(
            client_id,
            order_number,
            request_type=request_type,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        if not result.get("success"):
            return result
        requests = result.get("requests") or []
        if not requests:
            return {
                **result,
                "message": "No Shopify-native return request was found for this order.",
            }
        if len(requests) > 1:
            return {
                **result,
                "message": "Multiple Shopify-native return requests found for this order.",
            }
        request = requests[0]
        return {
            **result,
            "request": request,
            "message": f"Shopify return {request.get('request_number') or request.get('request_id')} is {request.get('status')}.",
        }

    async def get_request_by_id(self, client_id: str, request_id: str) -> dict:
        return {
            "success": False,
            "message": "Shopify-native return lookup by request id is not implemented yet; please query by order number.",
            "request_id": request_id,
        }

    async def get_portal_link(
        self,
        client_id: str,
        order_number: str,
        *,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        request_type: str | None = None,
        state: dict | None = None,
        order: dict | None = None,
        selected_line_items: list[dict] | None = None,
        return_reason: str | None = None,
        desired_resolution: str | None = None,
    ) -> dict:
        normalized_request_type = str(request_type or "return").strip().lower()
        native_config = await self._aget_shopify_native_config(client_id)
        create_enabled = bool(native_config.get("create_return_enabled"))
        exchange_create_enabled = bool(native_config.get("create_exchange_enabled"))

        if normalized_request_type == "exchange":
            if not exchange_create_enabled:
                return {
                    "success": True,
                    "eligible": True,
                    "partner": self.partner_name,
                    "order_name": self._normalize_order_name(order_number),
                    "portal_url": None,
                    "create_supported": True,
                    "create_enabled": False,
                    "message": "This client is configured for Shopify-native exchanges, but automatic exchange creation is disabled.",
                }
            create_result = await self._acreate_exchange(
                client_id=client_id,
                order_number=order_number,
                order=order,
                state=state,
                selected_line_items=selected_line_items,
                return_reason=return_reason,
                desired_resolution=desired_resolution,
                notify_customer=bool(native_config.get("notify_customer")),
            )
            return {
                **create_result,
                "partner": self.partner_name,
                "order_name": self._normalize_order_name(order_number),
                "portal_url": None,
                "create_supported": True,
                "create_enabled": True,
            }

        if not create_enabled:
            return {
                "success": True,
                "eligible": True,
                "partner": self.partner_name,
                "order_name": self._normalize_order_name(order_number),
                "portal_url": None,
                "create_supported": True,
                "create_enabled": False,
                "message": "This client is configured for Shopify-native returns, but automatic return creation is disabled.",
            }

        create_result = await self._acreate_return(
            client_id=client_id,
            order_number=order_number,
            order=order,
            state=state,
            selected_line_items=selected_line_items,
            return_reason=return_reason,
            notify_customer=bool(native_config.get("notify_customer")),
        )
        return {
            **create_result,
            "partner": self.partner_name,
            "order_name": self._normalize_order_name(order_number),
            "portal_url": None,
            "create_supported": True,
            "create_enabled": True,
        }

    async def _acreate_return(
        self,
        *,
        client_id: str,
        order_number: str,
        order: dict | None,
        state: dict | None,
        selected_line_items: list[dict] | None,
        return_reason: str | None,
        notify_customer: bool,
    ) -> dict:
        order_payload = order or await self._aget_order(client_id, order_number, state=state)
        order_gid = order_payload.get("admin_graphql_api_id") or order_payload.get("id")
        if not order_gid:
            return {
                "success": True,
                "eligible": False,
                "message": f"Could not find Shopify order {order_number}.",
            }

        from fashion_bot.shopify.modules.return_apis import (
            acreate_shopify_return,
            aget_order_fulfillment_line_items,
        )

        fulfillment_result = await aget_order_fulfillment_line_items(
            client_id=client_id,
            order_gid_or_id=order_gid,
        )
        if not fulfillment_result.get("success"):
            return {
                "success": False,
                "eligible": False,
                "message": "Could not fetch Shopify fulfillment line items for return creation.",
                "details": fulfillment_result,
            }

        candidates = self._returnable_fulfillment_items(fulfillment_result)
        selected = self._select_return_items(candidates, selected_line_items)
        if selected.get("needs_item_selection"):
            return selected
        return_line_items = [
            {
                "fulfillmentLineItemId": item["fulfillment_line_item_id"],
                "quantity": item["quantity"],
                "returnReason": self._shopify_return_reason(return_reason),
                "returnReasonNote": (return_reason or "")[:255],
            }
            for item in selected["items"]
        ]
        result = await acreate_shopify_return(
            client_id=client_id,
            order_gid_or_id=order_gid,
            return_line_items=return_line_items,
            notify_customer=notify_customer,
        )
        if not result.get("success"):
            return {
                "success": False,
                "eligible": True,
                "message": "Shopify return creation failed.",
                "details": result,
            }
        created_return = result.get("return") or {}
        return {
            "success": True,
            "eligible": True,
            "partner": self.partner_name,
            "created": True,
            "request": {
                "request_id": created_return.get("id"),
                "request_number": created_return.get("name"),
                "request_type": "return",
                "status": created_return.get("status"),
                "order_name": order_payload.get("name") or self._normalize_order_name(order_number),
            },
            "message": f"Shopify return {created_return.get('name') or created_return.get('id')} has been created.",
        }

    async def _acreate_exchange(
        self,
        *,
        client_id: str,
        order_number: str,
        order: dict | None,
        state: dict | None,
        selected_line_items: list[dict] | None,
        return_reason: str | None,
        desired_resolution: str | None,
        notify_customer: bool,
    ) -> dict:
        order_payload = order or await self._aget_order(client_id, order_number, state=state)
        order_gid = order_payload.get("admin_graphql_api_id") or order_payload.get("id")
        if not order_gid:
            return {
                "success": True,
                "eligible": False,
                "message": f"Could not find Shopify order {order_number}.",
            }

        from fashion_bot.shopify.modules.return_apis import (
            acreate_shopify_return,
            aget_order_fulfillment_line_items,
        )

        fulfillment_result = await aget_order_fulfillment_line_items(
            client_id=client_id,
            order_gid_or_id=order_gid,
        )
        if not fulfillment_result.get("success"):
            return {
                "success": False,
                "eligible": False,
                "message": "Could not fetch Shopify fulfillment line items for exchange creation.",
                "details": fulfillment_result,
            }

        candidates = self._returnable_fulfillment_items(fulfillment_result)
        selected = self._select_return_items(candidates, selected_line_items)
        if selected.get("needs_item_selection"):
            return {
                **selected,
                "message": selected.get("message") or "Please confirm which item you want to exchange.",
            }
        exchange_variant_id = self._exchange_variant_id(
            selected_line_items=selected_line_items,
            desired_resolution=desired_resolution,
        )
        if not exchange_variant_id:
            return {
                "success": True,
                "eligible": True,
                "needs_exchange_variant_selection": True,
                "message": "Please confirm the replacement product/variant for this exchange.",
            }

        return_line_items = [
            {
                "fulfillmentLineItemId": item["fulfillment_line_item_id"],
                "quantity": item["quantity"],
                "returnReason": self._shopify_return_reason(return_reason),
                "returnReasonNote": (return_reason or "")[:255],
            }
            for item in selected["items"]
        ]
        exchange_line_items = [
            {
                "variantId": self._variant_gid(exchange_variant_id),
                "quantity": max(1, int(item.get("quantity") or 1)),
            }
            for item in selected["items"]
        ]
        result = await acreate_shopify_return(
            client_id=client_id,
            order_gid_or_id=order_gid,
            return_line_items=return_line_items,
            exchange_line_items=exchange_line_items,
            notify_customer=notify_customer,
        )
        if not result.get("success"):
            return {
                "success": False,
                "eligible": True,
                "message": "Shopify exchange creation failed.",
                "details": result,
            }
        created_return = result.get("return") or {}
        return {
            "success": True,
            "eligible": True,
            "partner": self.partner_name,
            "created": True,
            "request": {
                "request_id": created_return.get("id"),
                "request_number": created_return.get("name"),
                "request_type": "exchange",
                "status": created_return.get("status"),
                "order_name": order_payload.get("name") or self._normalize_order_name(order_number),
                "exchange_line_items": ((created_return.get("exchangeLineItems") or {}).get("nodes")) or [],
            },
            "message": f"Shopify exchange {created_return.get('name') or created_return.get('id')} has been created.",
        }

    def _returnable_fulfillment_items(self, fulfillment_result: dict) -> list[dict]:
        order = ((fulfillment_result.get("data") or {}).get("order") or {})
        candidates: list[dict] = []
        for fulfillment in order.get("fulfillments") or []:
            for node in (((fulfillment.get("fulfillmentLineItems") or {}).get("nodes")) or []):
                line_item = node.get("lineItem") or {}
                product = line_item.get("product") or {}
                variant = line_item.get("variant") or {}
                fulfillment_line_item_id = node.get("id")
                quantity = int(node.get("quantity") or line_item.get("quantity") or 1)
                if fulfillment_line_item_id and quantity > 0:
                    candidates.append(
                        {
                            "fulfillment_line_item_id": fulfillment_line_item_id,
                            "quantity": quantity,
                            "line_item_id": line_item.get("id"),
                            "title": line_item.get("name"),
                            "product_id": product.get("id"),
                            "variant_id": variant.get("id"),
                            "variant_title": variant.get("title"),
                        }
                    )
        return candidates

    def _select_return_items(
        self,
        candidates: list[dict],
        selected_line_items: list[dict] | None,
    ) -> dict:
        if not candidates:
            return {
                "success": True,
                "eligible": False,
                "message": "No fulfilled Shopify line items are available for return creation.",
            }
        if not selected_line_items:
            if len(candidates) == 1:
                return {"success": True, "items": candidates}
            return {
                "success": True,
                "eligible": True,
                "needs_item_selection": True,
                "items": [
                    {
                        "title": item.get("title"),
                        "variant_title": item.get("variant_title"),
                        "quantity": item.get("quantity"),
                    }
                    for item in candidates
                ],
                "message": "Please confirm which item you want to return.",
            }

        selected: list[dict] = []
        selection_tokens = {
            str(value).strip().lower()
            for item in selected_line_items
            if isinstance(item, dict)
            for value in (
                item.get("id"),
                item.get("line_item_id"),
                item.get("product_id"),
                item.get("variant_id"),
                item.get("title"),
            )
            if value
        }
        for candidate in candidates:
            candidate_tokens = {
                str(value).strip().lower()
                for value in (
                    candidate.get("line_item_id"),
                    candidate.get("product_id"),
                    candidate.get("variant_id"),
                    candidate.get("title"),
                )
                if value
            }
            if selection_tokens & candidate_tokens:
                selected.append(candidate)
        if selected:
            return {"success": True, "items": selected}
        return {
            "success": True,
            "eligible": True,
            "needs_item_selection": True,
            "message": "I could not match the selected item to a fulfilled Shopify line item. Please confirm the item name.",
        }

    def _exchange_variant_id(
        self,
        *,
        selected_line_items: list[dict] | None,
        desired_resolution: str | None,
    ) -> str | None:
        for item in selected_line_items or []:
            if not isinstance(item, dict):
                continue
            value = _first_non_empty(
                item.get("exchange_variant_id"),
                item.get("replacement_variant_id"),
                item.get("new_variant_id"),
                item.get("desired_variant_id"),
            )
            if value:
                return str(value).strip()
        text = str(desired_resolution or "").strip()
        if text.startswith("gid://shopify/ProductVariant/"):
            return text
        return None

    def _variant_gid(self, variant_id: str) -> str:
        text = str(variant_id or "").strip()
        if text.startswith("gid://shopify/ProductVariant/"):
            return text
        return f"gid://shopify/ProductVariant/{text}"

    def _shopify_return_reason(self, reason: str | None) -> str:
        text = str(reason or "").lower()
        if any(token in text for token in ("damage", "defect", "broken")):
            return "DEFECTIVE"
        if "wrong" in text:
            return "WRONG_ITEM"
        if "small" in text:
            return "SIZE_TOO_SMALL"
        if "large" in text or "big" in text:
            return "SIZE_TOO_LARGE"
        return "OTHER"

    async def normalize_webhook(self, client_id: str, payload: dict, headers: dict) -> dict:
        return {
            "success": True,
            "partner": self.partner_name,
            "client_id": client_id,
            "event_type": payload.get("topic") or payload.get("event_type") or "shopify_return_event",
            "payload": payload,
        }


register_return_partner("shopify", ShopifyReturnPartnerService())
register_return_partner("shopify_native", ShopifyReturnPartnerService())
