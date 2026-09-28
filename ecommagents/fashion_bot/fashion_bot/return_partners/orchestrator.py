"""Generic return/exchange orchestration across return partners."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fashion_bot.return_partners.router import ReturnPartnerRouter
from fashion_bot.utils.phone_number_utils import is_real_phone_number
from fashion_bot.utils.utils import get_trace_id, log_with_trace_id

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS_CODES = {0, 408, 425, 429, 500, 502, 503, 504}
_PICKED_UP_STATUSES = {"picked up", "pickup completed", "in transit", "return in transit"}
_RTO_STATUSES = {"rto", "rto delivered", "return to origin", "returned to origin", "delivered"}
_PARTNER_IDENTITY_FALLBACK_REASONS = {"order_not_found", "order_lookup_failed"}


class ReturnPartnerOrchestrator:
    """Coordinates validation-lite partner calls, retries, and safe responses."""

    @staticmethod
    def _client_id(client_id: str | None, state: dict | None) -> str:
        return str(client_id or (state or {}).get("client_id") or "").strip()

    @staticmethod
    def _support_message() -> str:
        return (
            "I'm sorry, I couldn't fetch your return/exchange details right now. "
            "Please contact customer support and we'll help you with this."
        )

    @staticmethod
    def _first_non_empty(*values: Any) -> Any:
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return None

    @staticmethod
    def _resolve_request_from_status_result(status_result: dict) -> dict:
        """
        Pull a single, fully-detailed request object out of an aget_return_status
        result, whether it matched exactly one request (the "request" key) or
        several (the "requests_full" key, which — unlike the stripped "requests"
        summary list — carries refund/line_items/shipping data for each match).

        Falls back to the stripped summary only if full detail isn't available,
        so callers never silently lose refund/pickup data just because an order
        has more than one return/exchange request on it.
        """
        request = status_result.get("request")
        if isinstance(request, dict) and request:
            return request

        full_requests = [
            item for item in status_result.get("requests_full") or [] if isinstance(item, dict)
        ]
        if full_requests:
            return full_requests[0]

        summary_requests = [
            item for item in status_result.get("requests") or [] if isinstance(item, dict)
        ]
        return summary_requests[0] if summary_requests else {}

    @staticmethod
    def _customer_identity_from_inputs(
        *,
        state: dict | None,
        customer_phone: str | None,
        customer_email: str | None,
    ) -> tuple[str | None, str | None]:
        state = state or {}
        phone = ReturnPartnerOrchestrator._first_non_empty(
            customer_phone,
            state.get("phone_number"),
            state.get("customer_phone"),
            state.get("phone"),
            state.get("user_phone"),
            state.get("whatsapp_phone"),
        )
        email = ReturnPartnerOrchestrator._first_non_empty(
            customer_email,
            state.get("customer_email"),
            state.get("email"),
            state.get("user_email"),
        )
        # Same web-chat session-id problem as in ``identity.py``, but this value is
        # not the identity verdict — it becomes the ``customer_phone`` *filter* sent
        # to the return partner (and to the Redis cache key built from it). Passing
        # "fbw_..." through would be digit-stripped partner-side into a bogus
        # short numeric string that matches no request, so a web-chat customer who had
        # verified by EMAIL would still be told they have no return/exchange
        # requests. Drop the non-phone instead of filtering on it.
        if phone and not is_real_phone_number(str(phone)):
            phone = None
        return (
            str(phone).strip() if phone else None,
            str(email).strip() if email else None,
        )

    @staticmethod
    async def _aget_json_config(config_key: str, client_id: str | None) -> dict:
        try:
            from fashion_bot.config_manager import aget_json_config

            return await aget_json_config(config_key, client_id=client_id) or {}
        except Exception:
            return {}

    @staticmethod
    async def _return_rules_config(client_id: str | None) -> dict:
        return await ReturnPartnerOrchestrator._aget_json_config("return_exchange_rules", client_id)

    @staticmethod
    def _identity_required(rules_config: dict) -> bool:
        identity = rules_config.get("identity") if isinstance(rules_config.get("identity"), dict) else {}
        if "require_customer_identity" in identity:
            return bool(identity.get("require_customer_identity"))
        return True

    @staticmethod
    async def _verify_identity(
        *,
        client_id: str,
        order_number: str,
        state: dict | None,
        order: dict | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        rules_config = await ReturnPartnerOrchestrator._return_rules_config(client_id)
        from fashion_bot.return_partners.identity import averify_order_identity

        return await averify_order_identity(
            client_id=client_id,
            order_number=order_number,
            state=state,
            order=order,
            customer_phone=customer_phone,
            customer_email=customer_email,
            require_identity=ReturnPartnerOrchestrator._identity_required(rules_config),
        )

    @staticmethod
    def _should_retry(result: dict) -> bool:
        status_code = result.get("status_code")
        return result.get("success") is False and status_code in _TRANSIENT_STATUS_CODES

    @staticmethod
    async def _call_with_retry(
        operation: Callable[[], Awaitable[dict]],
        *,
        state: dict | None,
        operation_name: str,
        max_attempts: int = 2,
    ) -> dict:
        last_result: dict | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = await operation()
            except Exception as exc:
                result = {
                    "success": False,
                    "status_code": 0,
                    "message": str(exc),
                    "error": str(exc),
                }

            last_result = result
            if result.get("success") or not ReturnPartnerOrchestrator._should_retry(result):
                return result

            if attempt < max_attempts:
                log_with_trace_id(
                    state,
                    f"[RETURN_PARTNERS] retrying {operation_name} attempt={attempt + 1}",
                    "warning",
                )
                await asyncio.sleep(0.2 * attempt)

        return last_result or {
            "success": False,
            "status_code": 0,
            "message": "Return partner call failed.",
        }

    @staticmethod
    async def _raise_system_escalation(
        *,
        state: dict | None,
        category: str,
        reason: str,
        action_required: str,
        metadata: dict[str, Any],
    ) -> str | None:
        if not state:
            return None
        try:
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state

            return await alog_escalation_from_state(
                state=state,
                category=category,
                reason=reason,
                action_required=action_required,
                metadata={
                    **metadata,
                    "trace_id": get_trace_id(state),
                },
            )
        except Exception as exc:
            log_with_trace_id(
                state,
                f"[RETURN_PARTNERS] failed to raise system escalation: {exc}",
                "error",
            )
            return None

    @staticmethod
    async def _resolve_service(
        *,
        client_id: str,
        state: dict | None,
        partner: str | None,
    ) -> tuple[str | None, Any | None]:
        partner_name, service = await ReturnPartnerRouter.aresolve_partner(
            client_id=client_id,
            state=state,
            partner=partner,
        )
        if not service:
            return partner_name, None
        return partner_name, service

    @staticmethod
    async def aget_return_status(
        *,
        client_id: str | None,
        order_number: str,
        request_type: str | None = None,
        state: dict | None = None,
        partner: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {"success": False, "message": "Missing client_id for return lookup."}

        identity = await ReturnPartnerOrchestrator._verify_identity(
            client_id=cid,
            order_number=order_number,
            state=state,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        resolved_phone, resolved_email = ReturnPartnerOrchestrator._customer_identity_from_inputs(
            state=state,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        partner_name, service = await ReturnPartnerOrchestrator._resolve_service(
            client_id=cid,
            state=state,
            partner=partner,
        )
        if not service:
            return {
                "success": False,
                "message": "Return/exchange partner is not configured for this client.",
                "partner": partner_name,
            }

        if identity.get("should_block"):
            can_try_partner_identity = (
                identity.get("failed_reason") in _PARTNER_IDENTITY_FALLBACK_REASONS
                and bool(resolved_phone or resolved_email)
            )
            if can_try_partner_identity:
                partner_result = await ReturnPartnerOrchestrator._call_with_retry(
                    lambda: service.get_status_by_order_number(
                        cid,
                        order_number,
                        request_type=request_type,
                        customer_phone=resolved_phone,
                        customer_email=resolved_email,
                    ),
                    state=state,
                    operation_name="get_return_status_partner_identity_fallback",
                )
                if partner_result.get("success"):
                    partner_result.setdefault("partner", partner_name)
                    partner_result["identity_verified"] = True
                    partner_result["identity"] = {
                        "success": True,
                        "verified": True,
                        "should_block": False,
                        "matched_on": "return_partner_customer_contact",
                        "shopify_identity": identity,
                    }
                    return partner_result
            if identity.get("failed_reason") in _PARTNER_IDENTITY_FALLBACK_REASONS:
                return {
                    "success": True,
                    "identity_verified": False,
                    "needs_identity": True,
                    "message": (
                        "Please share the phone number or email linked to this return/exchange "
                        "so I can safely look it up."
                    ),
                    "identity": identity,
                }

            return {
                "success": True,
                "identity_verified": False,
                "needs_identity": identity.get("needs_identity", False),
                "message": identity.get("message"),
                "identity": identity,
            }

        result = await ReturnPartnerOrchestrator._call_with_retry(
            lambda: service.get_status_by_order_number(
                cid,
                order_number,
                request_type=request_type,
                customer_phone=resolved_phone,
                customer_email=resolved_email,
            ),
            state=state,
            operation_name="get_return_status",
        )
        result.setdefault("partner", partner_name)
        result.setdefault("identity_verified", True)
        if result.get("success"):
            return result

        escalation_id = await ReturnPartnerOrchestrator._raise_system_escalation(
            state=state,
            category="Return Partner Failure",
            reason="Return status lookup failed",
            action_required=f"Check {partner_name or 'return partner'} API for order {order_number}",
            metadata={
                "partner": partner_name,
                "order_number": order_number,
                "request_type": request_type,
                "result": result,
            },
        )
        return {
            **result,
            "success": False,
            "message": ReturnPartnerOrchestrator._support_message(),
            "escalation_id": escalation_id,
            "partner": partner_name,
        }

    @staticmethod
    async def alist_return_requests(
        *,
        client_id: str | None,
        order_number: str,
        request_type: str | None = None,
        state: dict | None = None,
        partner: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {"success": False, "message": "Missing client_id for return lookup."}
        identity = await ReturnPartnerOrchestrator._verify_identity(
            client_id=cid,
            order_number=order_number,
            state=state,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        resolved_phone, resolved_email = ReturnPartnerOrchestrator._customer_identity_from_inputs(
            state=state,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        if identity.get("should_block"):
            if identity.get("failed_reason") in _PARTNER_IDENTITY_FALLBACK_REASONS:
                if resolved_phone or resolved_email:
                    partner_name, service = await ReturnPartnerOrchestrator._resolve_service(
                        client_id=cid,
                        state=state,
                        partner=partner,
                    )
                    if service:
                        partner_result = await ReturnPartnerOrchestrator._call_with_retry(
                            lambda: service.list_requests_by_order_number(
                                cid,
                                order_number,
                                request_type=request_type,
                                customer_phone=resolved_phone,
                                customer_email=resolved_email,
                            ),
                            state=state,
                            operation_name="list_return_requests_partner_identity_fallback",
                        )
                        if partner_result.get("success"):
                            partner_result.setdefault("partner", partner_name)
                            partner_result["identity_verified"] = True
                            partner_result["identity"] = {
                                "success": True,
                                "verified": True,
                                "should_block": False,
                                "matched_on": "return_partner_customer_contact",
                                "shopify_identity": identity,
                            }
                            return partner_result
                return {
                    "success": True,
                    "identity_verified": False,
                    "needs_identity": True,
                    "message": (
                        "Please share the phone number or email linked to this return/exchange "
                        "so I can safely look it up."
                    ),
                    "identity": identity,
                }
            return {
                "success": True,
                "identity_verified": False,
                "needs_identity": identity.get("needs_identity", False),
                "message": identity.get("message"),
                "identity": identity,
            }
        partner_name, service = await ReturnPartnerOrchestrator._resolve_service(
            client_id=cid,
            state=state,
            partner=partner,
        )
        if not service:
            return {
                "success": False,
                "message": "Return/exchange partner is not configured for this client.",
                "partner": partner_name,
            }
        result = await ReturnPartnerOrchestrator._call_with_retry(
            lambda: service.list_requests_by_order_number(
                cid,
                order_number,
                request_type=request_type,
                customer_phone=resolved_phone,
                customer_email=resolved_email,
            ),
            state=state,
            operation_name="list_return_requests",
        )
        result.setdefault("partner", partner_name)
        result.setdefault("identity_verified", True)
        return result

    @staticmethod
    async def aget_return_or_exchange_portal(
        *,
        client_id: str | None,
        order_number: str,
        customer_email: str | None = None,
        request_type: str | None = None,
        state: dict | None = None,
        partner: str | None = None,
        customer_phone: str | None = None,
        selected_line_items: list[dict] | None = None,
        return_reason: str | None = None,
        proof_provided: bool | None = None,
        tag_intact_confirmed: bool | None = None,
        desired_resolution: str | None = None,
    ) -> dict:
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {"success": False, "message": "Missing client_id for return lookup."}

        identity = await ReturnPartnerOrchestrator._verify_identity(
            client_id=cid,
            order_number=order_number,
            state=state,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        if identity.get("should_block"):
            return {
                "success": True,
                "eligible": False,
                "identity_verified": False,
                "needs_identity": identity.get("needs_identity", False),
                "message": identity.get("message"),
                "identity": identity,
            }

        partner_name, service = await ReturnPartnerOrchestrator._resolve_service(
            client_id=cid,
            state=state,
            partner=partner,
        )
        if not service:
            return {
                "success": False,
                "message": "Return/exchange partner is not configured for this client.",
                "partner": partner_name,
            }
        resolved_phone, resolved_email = ReturnPartnerOrchestrator._customer_identity_from_inputs(
            state=state,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        validation = None
        if partner_name != "return_prime":
            validation = await ReturnPartnerOrchestrator.avalidate_return_exchange_rules(
                client_id=cid,
                order_number=order_number,
                request_type=request_type or "return",
                state=state,
                order=identity.get("order") or None,
                selected_line_items=selected_line_items,
                return_reason=return_reason,
                proof_provided=proof_provided,
                tag_intact_confirmed=tag_intact_confirmed,
                desired_resolution=desired_resolution,
            )
            if not validation.get("valid", False):
                return {
                    "success": True,
                    "eligible": False,
                    "validation": validation,
                    "message": validation.get("message")
                    or "This order is not eligible for return/exchange.",
                }
        result = await ReturnPartnerOrchestrator._call_with_retry(
            lambda: service.get_portal_link(
                cid,
                order_number,
                customer_email=resolved_email,
                customer_phone=resolved_phone,
                request_type=request_type,
                state=state,
                order=identity.get("order") or None,
                selected_line_items=selected_line_items,
                return_reason=return_reason,
                desired_resolution=desired_resolution,
            ),
            state=state,
            operation_name="get_return_portal",
        )
        result.setdefault("partner", partner_name)
        result.setdefault("identity_verified", True)
        if validation is not None:
            result.setdefault("validation", validation)
        return result

    @staticmethod
    async def avalidate_return_exchange_rules(
        *,
        client_id: str | None,
        order_number: str,
        request_type: str | None = None,
        state: dict | None = None,
        order: dict | None = None,
        selected_line_items: list[dict] | None = None,
        return_reason: str | None = None,
        proof_provided: bool | None = None,
        tag_intact_confirmed: bool | None = None,
        desired_resolution: str | None = None,
    ) -> dict:
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {
                "success": True,
                "valid": False,
                "message": "Missing client_id for return/exchange validation.",
                "failed_rules": ["missing_client_id"],
            }
        from fashion_bot.return_partners.rules import avalidate_return_exchange_request

        return await avalidate_return_exchange_request(
            client_id=cid,
            order_number=order_number,
            request_type=request_type,
            state=state,
            order=order,
            selected_line_items=selected_line_items,
            return_reason=return_reason,
            proof_provided=proof_provided,
            tag_intact_confirmed=tag_intact_confirmed,
            desired_resolution=desired_resolution,
        )

    @staticmethod
    async def aget_return_request_by_id(
        *,
        client_id: str | None,
        request_id: str,
        state: dict | None = None,
        partner: str | None = None,
    ) -> dict:
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {"success": False, "message": "Missing client_id for return lookup."}
        partner_name, service = await ReturnPartnerOrchestrator._resolve_service(
            client_id=cid,
            state=state,
            partner=partner,
        )
        if not service:
            return {
                "success": False,
                "message": "Return/exchange partner is not configured for this client.",
                "partner": partner_name,
            }
        result = await ReturnPartnerOrchestrator._call_with_retry(
            lambda: service.get_request_by_id(cid, request_id),
            state=state,
            operation_name="get_return_request_by_id",
        )
        result.setdefault("partner", partner_name)
        request = result.get("request") if isinstance(result.get("request"), dict) else {}
        order_name = request.get("order_name") or request.get("order_number")
        if result.get("success") and request and not order_name:
            # Fail closed. A request we cannot tie back to an order cannot be
            # identity-checked, and returning it anyway hands the customer name,
            # email, phone, items and refund to whoever quoted the request number.
            # Reachable via the cached webhook row (its order_number is nullable).
            log_with_trace_id(
                state,
                f"🚫 Return request {request_id} has no order reference; refusing without identity check",
                "warning",
            )
            return {
                "success": True,
                "identity_verified": False,
                "needs_identity": True,
                "message": (
                    "Please share the phone number or email linked to this order so I can verify it."
                ),
                "partner": partner_name,
            }
        if result.get("success") and order_name:
            identity = await ReturnPartnerOrchestrator._verify_identity(
                client_id=cid,
                order_number=str(order_name),
                state=state,
            )
            if identity.get("should_block"):
                return {
                    "success": True,
                    "identity_verified": False,
                    "needs_identity": identity.get("needs_identity", False),
                    "message": identity.get("message"),
                    "identity": identity,
                    "partner": partner_name,
                }
            result.setdefault("identity_verified", True)
        return result

    @staticmethod
    def _first_non_empty(*values: Any) -> Any:
        for value in values:
            if value not in (None, "", [], {}):
                return value
        return None

    @staticmethod
    def _extract_return_shipping(request: dict) -> dict:
        raw = request.get("raw") if isinstance(request.get("raw"), dict) else request
        line_items = raw.get("line_items") or raw.get("items") or []
        if not isinstance(line_items, list):
            line_items = []

        for item in line_items:
            if not isinstance(item, dict):
                continue
            shipping_entries = item.get("shipping") or []
            if isinstance(shipping_entries, dict):
                shipping_entries = [shipping_entries]
            if not isinstance(shipping_entries, list):
                continue
            for shipping in shipping_entries:
                if not isinstance(shipping, dict):
                    continue
                labels = shipping.get("labels") or []
                label = labels[0] if labels and isinstance(labels[0], dict) else {}
                return {
                    "awb": ReturnPartnerOrchestrator._first_non_empty(
                        shipping.get("awb"),
                        shipping.get("tracking_number"),
                        label.get("awb"),
                        label.get("tracking_number"),
                    ),
                    "tracking_url": ReturnPartnerOrchestrator._first_non_empty(
                        shipping.get("tracking_url"),
                        label.get("tracking_url"),
                        label.get("label_url"),
                    ),
                    "raw_status": ReturnPartnerOrchestrator._first_non_empty(
                        shipping.get("status"),
                        shipping.get("delivery_status"),
                        label.get("status"),
                    ),
                    "carrier": ReturnPartnerOrchestrator._first_non_empty(
                        shipping.get("carrier"),
                        label.get("carrier"),
                    ),
                    "raw": shipping,
                }
        delivery = request.get("delivery") if isinstance(request.get("delivery"), dict) else {}
        return {
            "awb": None,
            "tracking_url": None,
            "raw_status": delivery.get("status"),
            "carrier": None,
            "raw": delivery,
        }

    @staticmethod
    def _classify_return_pickup_status(raw_status: str | None, request_status: str | None) -> dict:
        status_text = str(raw_status or request_status or "").strip()
        normalized = status_text.lower().replace("_", " ")
        if not normalized:
            return {
                "status": "unknown",
                "message": "I could not find a pickup status for this return yet.",
            }
        if any(token in normalized for token in ("exception", "failed", "cancel")):
            return {
                "status": "pickup_issue",
                "message": "There seems to be an issue with the return pickup. Our support team can help you with this.",
            }
        if normalized in _RTO_STATUSES or "return to origin" in normalized or "rto" in normalized:
            return {
                "status": "return_to_origin",
                "message": "Your returned item has reached or is reaching the origin facility.",
            }
        if normalized in _PICKED_UP_STATUSES or "picked" in normalized:
            return {
                "status": "picked_up",
                "message": "Your return pickup is completed and the item is in transit.",
            }
        if "out for pickup" in normalized:
            return {
                "status": "out_for_pickup",
                "message": "Your return pickup is out for pickup.",
            }
        if "scheduled" in normalized:
            return {
                "status": "pickup_scheduled",
                "message": "Your return pickup has been scheduled.",
            }
        if "requested" in normalized or "approved" in normalized:
            return {
                "status": "request_under_process",
                "message": "Your return request is under process. Pickup details will be updated once scheduled.",
            }
        return {
            "status": normalized,
            "message": f"Your return pickup status is {status_text}.",
        }

    @staticmethod
    async def aget_return_pickup_status(
        *,
        client_id: str | None,
        order_number: str,
        state: dict | None = None,
        partner: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        status_result = await ReturnPartnerOrchestrator.aget_return_status(
            client_id=client_id,
            order_number=order_number,
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        # An identity block is reported as ``success: True`` with
        # ``identity_verified: False`` (see ``aget_return_status``), so checking
        # ``success`` alone let a blocked lookup fall through to the enrichment
        # below with an empty request — replacing "please share your phone number"
        # with a misleading "I could not find a pickup status for this return yet."
        # Mirror the guard ``aget_refund_status`` and ``aget_exchange_delivery_status``
        # already use.
        if not status_result.get("success") or status_result.get("identity_verified") is False:
            return status_result

        request = ReturnPartnerOrchestrator._resolve_request_from_status_result(status_result)
        from fashion_bot.return_partners.shipments import aenrich_return_pickup_leg

        pickup = await aenrich_return_pickup_leg(request=request, state=state)
        return {
            "success": True,
            "partner": status_result.get("partner"),
            "identity_verified": status_result.get("identity_verified"),
            "order_name": status_result.get("order_name") or request.get("order_name"),
            "request": request,
            "pickup": pickup,
            "message": pickup["message"],
        }

    @staticmethod
    async def aget_refund_status(
        *,
        client_id: str | None,
        order_number: str,
        state: dict | None = None,
        partner: str | None = None,
        request_type: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {"success": False, "message": "Missing client_id for refund lookup."}
        status_result = await ReturnPartnerOrchestrator.aget_return_status(
            client_id=cid,
            order_number=order_number,
            request_type=request_type or "return",
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        if not status_result.get("success") or status_result.get("identity_verified") is False:
            return status_result

        request = ReturnPartnerOrchestrator._resolve_request_from_status_result(status_result)
        from fashion_bot.return_partners.refunds import aget_refund_visibility

        refund = await aget_refund_visibility(
            client_id=cid,
            request=request,
            request_type=request_type or request.get("request_type") or "return",
        )
        escalation_id = None
        if refund.get("should_escalate"):
            escalation_id = await ReturnPartnerOrchestrator._raise_system_escalation(
                state=state,
                category="Return Refund SLA",
                reason="Refund SLA appears breached",
                action_required=f"Check refund status for order {order_number}",
                metadata={
                    "order_number": order_number,
                    "request_number": request.get("request_number"),
                    "refund": refund,
                },
            )
        return {
            "success": True,
            "partner": status_result.get("partner"),
            "identity_verified": True,
            "order_name": status_result.get("order_name") or request.get("order_name"),
            "request": request,
            "refund": refund,
            "escalation_id": escalation_id,
            "message": refund.get("message"),
        }

    @staticmethod
    def _exchange_order_from_request(request: dict) -> dict:
        exchange_order = request.get("exchange_order")
        if isinstance(exchange_order, dict):
            return exchange_order
        raw = request.get("raw") if isinstance(request.get("raw"), dict) else request
        exchange = raw.get("exchange") if isinstance(raw.get("exchange"), dict) else {}
        order = exchange.get("order") if isinstance(exchange.get("order"), dict) else {}
        return {
            "id": ReturnPartnerOrchestrator._first_non_empty(exchange.get("order_id"), order.get("id")),
            "name": ReturnPartnerOrchestrator._first_non_empty(exchange.get("order_name"), order.get("name")),
        }

    @staticmethod
    def _can_create_exchange_for_status(policy: str, pickup_status: str) -> bool:
        normalized_policy = str(policy or "").strip().lower()
        normalized_status = str(pickup_status or "").strip().lower()
        if normalized_policy == "on_pickup":
            return normalized_status in {"picked_up", "return_to_origin"}
        if normalized_policy == "on_rto":
            return normalized_status == "return_to_origin"
        if normalized_policy == "existing_customer_immediate":
            return True
        return False

    @staticmethod
    async def aensure_exchange_order(
        *,
        client_id: str | None,
        order_number: str,
        state: dict | None = None,
        partner: str | None = None,
        force: bool = False,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {"success": False, "message": "Missing client_id for exchange automation."}

        pickup_result = await ReturnPartnerOrchestrator.aget_return_pickup_status(
            client_id=cid,
            order_number=order_number,
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        # ``aget_return_pickup_status`` also returns ``success: True`` on an
        # identity block, so checking ``success`` alone let a blocked lookup reach
        # the request_type check below with an empty request and answer "This
        # return request is not an exchange request." — a false statement to a
        # customer who simply had not been asked to identify themselves yet.
        if not pickup_result.get("success") or pickup_result.get("identity_verified") is False:
            return pickup_result

        request = pickup_result.get("request") or {}
        if request.get("request_type") != "exchange":
            return {
                "success": False,
                "exchange_created": False,
                "message": "This return request is not an exchange request.",
                "request": request,
            }

        validation = await ReturnPartnerOrchestrator.avalidate_return_exchange_rules(
            client_id=cid,
            order_number=order_number,
            request_type="exchange",
            state=state,
            order=request,
        )
        if not validation.get("valid", False):
            return {
                "success": True,
                "exchange_created": False,
                "eligible": False,
                "validation": validation,
                "message": validation.get("message")
                or "This order is not eligible for exchange.",
                "request": request,
            }

        existing_exchange = ReturnPartnerOrchestrator._exchange_order_from_request(request)
        if existing_exchange.get("name") or existing_exchange.get("id"):
            return {
                "success": True,
                "exchange_created": False,
                "already_exists": True,
                "exchange_order": existing_exchange,
                "message": f"Exchange order {existing_exchange.get('name') or existing_exchange.get('id')} is already created.",
                "request": request,
            }

        policy_config = await ReturnPartnerOrchestrator._aget_json_config(
            "return_exchange_automation",
            cid,
        )
        policy = policy_config.get("exchange_creation_policy") or "manual_after_inspection"
        auto_enabled = bool(policy_config.get("auto_create_exchange_order")) or force
        pickup_status = ((pickup_result.get("pickup") or {}).get("status") or "").strip()

        if not auto_enabled:
            return {
                "success": True,
                "exchange_created": False,
                "automation_enabled": False,
                "message": "Exchange order automation is not enabled for this client.",
                "policy": policy,
                "pickup_status": pickup_status,
                "request": request,
            }

        if not ReturnPartnerOrchestrator._can_create_exchange_for_status(policy, pickup_status):
            return {
                "success": True,
                "exchange_created": False,
                "message": "Exchange order is not due for creation yet based on the configured policy.",
                "policy": policy,
                "pickup_status": pickup_status,
                "request": request,
            }

        return await ReturnPartnerOrchestrator._create_exchange_order_from_request(
            client_id=cid,
            order_number=order_number,
            request=request,
            policy=policy,
            pickup_status=pickup_status,
            state=state,
        )

    @staticmethod
    async def arequest_exchange_size_change(
        *,
        client_id: str | None,
        order_number: str,
        desired_size: str,
        state: dict | None = None,
        partner: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        """Capture a customer's requested size change on an existing exchange.

        Return Prime's API is read-only for return/exchange requests — there is
        no create/update call available to us, and this client does not have
        exchange-order automation enabled either. So this cannot change the
        size itself; it captures the request clearly (Shopify note + a
        structured escalation) so a human can make the correction in Return
        Prime's dashboard.
        """
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {"success": False, "message": "Missing client_id for exchange size change."}

        desired_size = str(desired_size or "").strip()
        if not desired_size:
            return {"success": False, "message": "No size was provided for the exchange."}

        status_result = await ReturnPartnerOrchestrator.aget_return_status(
            client_id=cid,
            order_number=order_number,
            request_type="exchange",
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        if status_result.get("identity_verified") is False or status_result.get("needs_identity"):
            return status_result
        if not status_result.get("success"):
            return status_result

        request = ReturnPartnerOrchestrator._resolve_request_from_status_result(status_result)
        request_number = request.get("request_number")

        if not request:
            # No existing exchange request to correct the size on — this is
            # actually a request to START one. Route into the real
            # eligibility-check-and-create flow instead of silently logging a
            # "size change" note/escalation against a request that doesn't
            # exist (which used to happen unconditionally here, leaving
            # customers with no real exchange request and no eligibility
            # answer — just a note nobody could act on).
            return await ReturnPartnerOrchestrator.aget_return_or_exchange_portal(
                client_id=cid,
                order_number=order_number,
                customer_email=customer_email,
                request_type="exchange",
                state=state,
                partner=partner,
                customer_phone=customer_phone,
                desired_resolution=f"Exchange for size {desired_size}",
            )

        await ReturnPartnerOrchestrator._add_shopify_note(
            order_number,
            (
                f"[Bloomerce] Customer requested a different exchange size: {desired_size} "
                f"(exchange request {request_number or 'unknown'}). Return Prime does not "
                "support editing the request via API — please update it manually."
            ),
            state,
        )
        escalation_id = await ReturnPartnerOrchestrator._raise_system_escalation(
            state=state,
            category="Exchange Size Change",
            reason="Customer wants a different size than what was originally requested for their exchange",
            action_required=(
                f"Update exchange request {request_number or '(number unknown)'} for order "
                f"{order_number} to size {desired_size} in Return Prime"
            ),
            metadata={
                "order_number": order_number,
                "request_number": request_number,
                "requested_size": desired_size,
                "request": request,
            },
        )

        return {
            "success": True,
            "escalation_id": escalation_id,
            "order_number": order_number,
            "request_number": request_number,
            "requested_size": desired_size,
            "message": (
                f"Noted — I've flagged your order ({order_number}) for a size {desired_size} "
                "exchange instead, with your order details, to our team so they can update it "
                "on their end."
            ),
        }

    @staticmethod
    async def aget_exchange_delivery_status(
        *,
        client_id: str | None,
        order_number: str,
        state: dict | None = None,
        partner: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {"success": False, "message": "Missing client_id for exchange delivery lookup."}

        status_result = await ReturnPartnerOrchestrator.aget_return_status(
            client_id=cid,
            order_number=order_number,
            request_type="exchange",
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )
        if status_result.get("identity_verified") is False or status_result.get("needs_identity"):
            return status_result
        if not status_result.get("success"):
            return status_result

        request = ReturnPartnerOrchestrator._resolve_request_from_status_result(status_result)
        if request.get("request_type") and request.get("request_type") != "exchange":
            return {
                "success": True,
                "exchange_order_exists": False,
                "message": "This return request is not an exchange request.",
                "request": request,
                "return_status": status_result,
            }

        exchange_order = ReturnPartnerOrchestrator._exchange_order_from_request(request)
        exchange_order_number = ReturnPartnerOrchestrator._first_non_empty(
            exchange_order.get("name"),
            exchange_order.get("order_number"),
            exchange_order.get("id"),
        )
        if not exchange_order_number:
            from fashion_bot.return_partners.shipments import aenrich_return_pickup_leg
            from fashion_bot.return_partners.stock import acheck_shopify_variant_stock

            pickup = await aenrich_return_pickup_leg(request=request, state=state)
            pickup_status = str((pickup or {}).get("status") or "").strip().lower()
            if pickup_status in {"picked_up", "return_to_origin"}:
                exchange_items = ReturnPartnerOrchestrator._exchange_line_items_from_request(request)
                stock = await acheck_shopify_variant_stock(
                    client_id=cid,
                    line_items=exchange_items,
                    state=state,
                )
                stock_available = stock.get("stock_available")
                if stock_available is False:
                    escalation_reason = (
                        "Return pickup is complete but exchange order is missing "
                        "and requested exchange stock is unavailable"
                    )
                elif stock_available is True:
                    escalation_reason = (
                        "Return pickup is complete and exchange stock is available, "
                        "but exchange order is missing"
                    )
                else:
                    escalation_reason = (
                        "Return pickup is complete but exchange order is missing "
                        "and exchange stock could not be verified"
                    )

                support_email = "support@groovee.in"
                try:
                    from fashion_bot.return_partners.instructions import aget_return_prime_instruction_config

                    instruction_config = await aget_return_prime_instruction_config(
                        client_id=cid,
                        request_type="exchange",
                    )
                    support_email = instruction_config.get("support_email") or support_email
                except Exception:
                    pass

                escalation_id = await ReturnPartnerOrchestrator._raise_system_escalation(
                    state=state,
                    category="Exchange Order Not Created",
                    reason=escalation_reason,
                    action_required=f"Review and create/resolve exchange order for {order_number}",
                    metadata={
                        "order_number": order_number,
                        "request_number": request.get("request_number"),
                        "request_id": request.get("request_id"),
                        "pickup": pickup,
                        "stock": {
                            key: stock.get(key)
                            for key in ("success", "stock_available", "message", "variants", "error")
                        },
                    },
                )
                if stock_available is False:
                    stock_sentence = "The requested exchange item is not currently available in stock."
                elif stock_available is True:
                    stock_sentence = "The requested exchange item is currently available in stock."
                else:
                    stock_sentence = "I could not verify the requested exchange item stock right now."
                return {
                    "success": True,
                    "exchange_order_exists": False,
                    "requires_manual_intervention": True,
                    "escalation_id": escalation_id,
                    "pickup": pickup,
                    "stock": stock,
                    "message": (
                        "Your return pickup is completed, but the exchange order has not been created yet. "
                        f"{stock_sentence} I have raised this with support for manual review. "
                        f"Please contact {support_email} for faster help."
                    ),
                    "request": request,
                    "return_status": status_result,
                }

            return {
                "success": True,
                "exchange_order_exists": False,
                "message": (
                    "Your exchange request is present, but the exchange order has not "
                    "been created yet."
                ),
                "request": request,
                "return_status": status_result,
            }

        try:
            from fashion_bot.core.orchestrator import OrderStatusOrchestrator

            delivery_status = await OrderStatusOrchestrator.aget_order_status(
                str(exchange_order_number),
                state=state,
            )
        except Exception as exc:
            delivery_status = {"success": False, "message": str(exc), "error": str(exc)}

        return {
            "success": True,
            "exchange_order_exists": True,
            "exchange_order": exchange_order,
            "exchange_delivery_status": delivery_status,
            "request": request,
            "return_status": status_result,
            "message": (
                f"Exchange order {exchange_order_number} is created. "
                "Here is the latest delivery status I found."
            ),
        }

    @staticmethod
    async def aget_return_exchange_instructions(
        *,
        client_id: str | None,
        request_type: str | None = None,
        state: dict | None = None,
    ) -> dict:
        cid = ReturnPartnerOrchestrator._client_id(client_id, state)
        if not cid:
            return {"success": False, "message": "Missing client_id for return/exchange instructions."}
        from fashion_bot.return_partners.instructions import aget_return_exchange_request_instructions

        return await aget_return_exchange_request_instructions(
            client_id=cid,
            request_type=request_type,
        )

    @staticmethod
    def _exchange_line_items_from_request(request: dict) -> list[dict]:
        raw = request.get("raw") if isinstance(request.get("raw"), dict) else request
        line_items = raw.get("line_items") or []
        if not isinstance(line_items, list):
            return []
        new_items: list[dict] = []
        for item in line_items:
            if not isinstance(item, dict):
                continue
            exchange = item.get("exchange") if isinstance(item.get("exchange"), dict) else {}
            exchange_product = (
                item.get("exchange_product")
                if isinstance(item.get("exchange_product"), dict)
                else {}
            )
            variant_id = ReturnPartnerOrchestrator._first_non_empty(
                exchange.get("variant_id"),
                exchange_product.get("variant_id"),
                exchange_product.get("id"),
            )
            if not variant_id:
                continue
            new_items.append(
                {
                    "variant_id": str(variant_id),
                    "quantity": int(item.get("quantity") or 1),
                }
            )
        return new_items

    @staticmethod
    async def _create_exchange_order_from_request(
        *,
        client_id: str,
        order_number: str,
        request: dict,
        policy: str,
        pickup_status: str,
        state: dict | None,
    ) -> dict:
        new_line_items = ReturnPartnerOrchestrator._exchange_line_items_from_request(request)
        if not new_line_items:
            escalation_id = await ReturnPartnerOrchestrator._raise_system_escalation(
                state=state,
                category="Exchange Order Automation",
                reason="Exchange order could not be created because exchange variant data is missing",
                action_required=f"Create exchange order manually for {order_number}",
                metadata={
                    "order_number": order_number,
                    "request_number": request.get("request_number"),
                    "policy": policy,
                    "pickup_status": pickup_status,
                },
            )
            await ReturnPartnerOrchestrator._add_shopify_note(
                order_number,
                (
                    "[Bloomerce] Exchange automation skipped: missing exchange "
                    f"variant data for request {request.get('request_number')}."
                ),
                state,
            )
            return {
                "success": False,
                "exchange_created": False,
                "requires_manual_intervention": True,
                "escalation_id": escalation_id,
                "message": "Exchange order needs manual support because variant data is missing.",
                "request": request,
            }

        try:
            from fashion_bot.core.factory import ServiceFactory

            order_service = await ServiceFactory.aget_order_service(
                client_id=client_id,
                state=state,
                vendor="shopify",
            )
            original_order = await order_service.aget_order_details(order_number, state=state)
            if not original_order:
                raise ValueError(f"Original Shopify order {order_number} not found")

            create_result = await order_service.aclone_order(
                original_order_data=original_order,
                new_line_items=new_line_items,
                state=state,
                note=(
                    "[Bloomerce] Exchange order auto-created after return "
                    f"pickup_status={pickup_status}, policy={policy}, "
                    f"return_request={request.get('request_number')}."
                ),
                additional_tags=[f"return_exchange_{policy}", "bloomerce_exchange_auto"],
            )
        except Exception as exc:
            create_result = {"success": False, "error": str(exc)}

        if create_result.get("success"):
            new_order_id = create_result.get("order_name") or create_result.get("order_id")
            return {
                "success": True,
                "exchange_created": True,
                "exchange_order": {"name": new_order_id, "id": create_result.get("order_id")},
                "message": f"Exchange order {new_order_id} has been created.",
                "request": request,
                "create_result": create_result,
            }

        escalation_id = await ReturnPartnerOrchestrator._raise_system_escalation(
            state=state,
            category="Exchange Order Automation",
            reason="Exchange order creation failed",
            action_required=f"Create exchange order manually for {order_number}",
            metadata={
                "order_number": order_number,
                "request_number": request.get("request_number"),
                "policy": policy,
                "pickup_status": pickup_status,
                "create_result": create_result,
            },
        )
        await ReturnPartnerOrchestrator._add_shopify_note(
            order_number,
            (
                "[Bloomerce] Exchange automation failed for request "
                f"{request.get('request_number')}: {create_result.get('error')}"
            ),
            state,
        )
        return {
            "success": False,
            "exchange_created": False,
            "requires_manual_intervention": True,
            "escalation_id": escalation_id,
            "message": "Exchange order creation failed and has been escalated to support.",
            "request": request,
            "create_result": create_result,
        }

    @staticmethod
    async def _add_shopify_note(order_number: str, note: str, state: dict | None) -> None:
        """Stamp a return/exchange automation failure onto the order.

        Every call site here pairs the note with ``_raise_system_escalation``,
        so the note is an escalation footprint and is suppressed for a client
        with ``escalation_policy.order_notes_enabled = false``. The escalation
        itself is unaffected.
        """
        try:
            from fashion_bot.core.factory import ServiceFactory
            from fashion_bot.utils.order_utils import aadd_escalation_order_note

            service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
            await aadd_escalation_order_note(service, order_number, note, state=state)
        except Exception as exc:
            log_with_trace_id(
                state,
                f"[RETURN_PARTNERS] failed to add Shopify note for {order_number}: {exc}",
                "warning",
            )
