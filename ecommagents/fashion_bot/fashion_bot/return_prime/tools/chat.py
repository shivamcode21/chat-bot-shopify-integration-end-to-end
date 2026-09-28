"""LangChain chat tools for Return Prime return/exchange lookups."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from fashion_bot.return_prime.workflow.service import return_prime_workflow


def _client_id_from_state(state: dict | None) -> str:
    return str((state or {}).get("client_id") or "").strip()


def _phone_from_state(state: dict | None) -> str:
    for key in ("phone_number", "customer_phone", "phone"):
        value = (state or {}).get(key)
        if value:
            return str(value).strip()
    return ""


def _email_from_state(state: dict | None) -> str:
    for key in ("customer_email", "email", "user_email"):
        value = (state or {}).get(key)
        if value:
            return str(value).strip()
    return ""


def create_return_prime_chat_tools(state: dict | None) -> list[Any]:
    """Create stateless tools for Return Prime status and portal lookups."""

    @tool
    async def get_return_prime_status_by_order_number(
        order_number: str,
        customer_email: str = "",
        customer_phone: str = "",
        request_type: str = "",
    ) -> dict:
        """
        Use this when the customer asks for return/exchange status for an order.

        Examples: "what is my return status", "where is my exchange order",
        "is my return under process", "status for return on order #1234".
        """
        client_id = _client_id_from_state(state)
        if not client_id:
            return {
                "success": False,
                "message": "Missing client_id for Return Prime lookup.",
            }
        return await return_prime_workflow.get_status_by_order_number(
            client_id,
            order_number,
            customer_email=customer_email or _email_from_state(state) or None,
            customer_phone=customer_phone or _phone_from_state(state) or None,
            request_type=request_type or None,
        )

    @tool
    async def list_return_prime_requests_by_order_number(
        order_number: str,
        customer_email: str = "",
        customer_phone: str = "",
        request_type: str = "",
    ) -> dict:
        """
        List all Return Prime return/exchange requests for an order.

        Use this if the customer may have multiple return/exchange requests on
        the same order, or asks which items/requests exist.
        """
        client_id = _client_id_from_state(state)
        if not client_id:
            return {
                "success": False,
                "message": "Missing client_id for Return Prime lookup.",
            }
        return await return_prime_workflow.list_requests_by_order_number(
            client_id,
            order_number,
            customer_email=customer_email or _email_from_state(state) or None,
            customer_phone=customer_phone or _phone_from_state(state) or None,
            request_type=request_type or None,
        )

    @tool
    async def get_return_prime_request_by_id(request_id: str) -> dict:
        """
        Fetch exact Return Prime request details when a return/exchange request id
        or request number is already known.
        """
        client_id = _client_id_from_state(state)
        if not client_id:
            return {
                "success": False,
                "message": "Missing client_id for Return Prime lookup.",
            }
        return await return_prime_workflow.get_request_by_id(client_id, request_id)

    @tool
    async def get_return_prime_portal_link(
        order_number: str,
        customer_email: str = "",
    ) -> dict:
        """
        Build the Return Prime portal link when the customer wants to start a new
        return or exchange request for an order.
        """
        client_id = _client_id_from_state(state)
        if not client_id:
            return {
                "success": False,
                "message": "Missing client_id for Return Prime lookup.",
            }
        return await return_prime_workflow.get_return_portal_link(
            client_id,
            order_number,
            customer_email=customer_email or None,
        )

    return [
        get_return_prime_status_by_order_number,
        list_return_prime_requests_by_order_number,
        get_return_prime_request_by_id,
        get_return_prime_portal_link,
    ]
