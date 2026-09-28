"""Return Prime implementation of the generic return partner interface."""

from __future__ import annotations

from fashion_bot.return_partners.registry import register_return_partner
from fashion_bot.return_prime.webhook.service import return_prime_webhook_service
from fashion_bot.return_prime.workflow.service import return_prime_workflow


class ReturnPrimePartnerService:
    """Stateless Return Prime service exposed behind the return partner router."""

    partner_name = "return_prime"

    async def get_status_by_order_number(
        self,
        client_id: str,
        order_number: str,
        *,
        request_type: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        return await return_prime_workflow.get_status_by_order_number(
            client_id,
            order_number,
            request_type=request_type,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

    async def list_requests_by_order_number(
        self,
        client_id: str,
        order_number: str,
        *,
        request_type: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        return await return_prime_workflow.list_requests_by_order_number(
            client_id,
            order_number,
            request_type=request_type,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

    async def get_request_by_id(self, client_id: str, request_id: str) -> dict:
        return await return_prime_workflow.get_request_by_id(client_id, request_id)

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
        existing = await return_prime_workflow.list_requests_by_order_number(
            client_id,
            order_number,
            customer_email=customer_email,
            customer_phone=customer_phone,
        )
        if not existing.get("success"):
            return {
                "success": False,
                "eligible": False,
                "portal_url": None,
                "order_name": order_number,
                "existing_request_check_failed": True,
                "message": (
                    "I couldn't verify whether this order already has a return/exchange request. "
                    "Please try again in a few minutes."
                ),
                "details": existing,
            }
        if existing.get("success") and existing.get("requests"):
            requests = [
                item for item in existing.get("requests") or []
                if isinstance(item, dict)
            ]
            return self._existing_request_response(
                order_number=existing.get("order_name") or order_number,
                requests=requests,
                source=existing.get("source"),
            )

        from fashion_bot.return_prime.workflow.rules import avalidate_return_prime_rules

        validation = await avalidate_return_prime_rules(
            client_id=client_id,
            order_number=order_number,
            request_type=request_type or "return",
            state=state,
            order=order,
            selected_line_items=selected_line_items,
        )
        if not validation.get("valid", False):
            return {
                "success": True,
                "eligible": False,
                "validation": validation,
                "message": validation.get("message") or "This order is not eligible for return/exchange.",
            }

        result = await return_prime_workflow.get_return_portal_link(
            client_id,
            order_number,
            customer_email=customer_email,
        )
        return {**result, "validation": validation}

    def _existing_request_response(
        self,
        *,
        order_number: str,
        requests: list[dict],
        source: str | None,
    ) -> dict:
        if len(requests) == 1:
            request = requests[0]
            request_type = str(request.get("request_type") or "return/exchange").replace("_", " ")
            request_ref = request.get("request_number") or request.get("request_id") or "this request"
            status = request.get("status") or "created"
            return {
                "success": True,
                "eligible": False,
                "already_exists": True,
                "portal_url": None,
                "order_name": order_number,
                "request": request,
                "source": source,
                "message": (
                    f"A {request_type} request already exists for order {order_number}: "
                    f"{request_ref} is {status}. Please use the existing request status instead of creating a new one."
                ),
            }

        return {
            "success": True,
            "eligible": False,
            "already_exists": True,
            "portal_url": None,
            "order_name": order_number,
            "requests": [
                {
                    "request_id": item.get("request_id"),
                    "request_number": item.get("request_number"),
                    "request_type": item.get("request_type"),
                    "status": item.get("status"),
                }
                for item in requests
            ],
            "source": source,
            "message": (
                f"Multiple return/exchange requests already exist for order {order_number}. "
                "Please use the existing request status instead of creating a new one."
            ),
        }

    async def normalize_webhook(self, client_id: str, payload: dict, headers: dict) -> dict:
        extracted = return_prime_webhook_service.extract_webhook_fields(payload)
        return {
            "partner": self.partner_name,
            "client_id": client_id,
            "payload": payload,
            "headers": headers,
            **extracted,
        }


return_prime_partner_service = ReturnPrimePartnerService()
register_return_partner(ReturnPrimePartnerService.partner_name, return_prime_partner_service)
