"""Generic LangChain tools for return/exchange partner workflows."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from fashion_bot.utils.order_access import order_access_guarded


def _client_id_from_state(state: dict | None) -> str:
    return str((state or {}).get("client_id") or "").strip()


def create_return_partner_chat_tools(state: dict | None) -> list[Any]:
    """Create stateless generic return/exchange tools for chat."""

    @tool
    @order_access_guarded(state)
    async def get_return_status_by_order_number(
        order_number: str,
        request_type: str = "",
        customer_phone: str = "",
        customer_email: str = "",
        verification_identifier_type: str = "",
        verification_identifier_value: str = "",
    ) -> dict:
        """
        Use this when the customer asks for return/exchange status for an order.

        Examples: "what is my return status", "where is my exchange order",
        "is my return under process", "status for return on order #1234".
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.aget_return_status(
            client_id=_client_id_from_state(state),
            order_number=order_number,
            request_type=request_type or None,
            state=state,
            customer_phone=customer_phone or None,
            customer_email=customer_email or None,
        )

    @tool
    @order_access_guarded(state)
    async def list_return_requests_by_order_number(
        order_number: str,
        request_type: str = "",
        customer_phone: str = "",
        customer_email: str = "",
        verification_identifier_type: str = "",
        verification_identifier_value: str = "",
    ) -> dict:
        """
        List all return/exchange requests for an order.

        Use this when the customer may have multiple return/exchange requests on
        one order or asks which items/requests exist.
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.alist_return_requests(
            client_id=_client_id_from_state(state),
            order_number=order_number,
            request_type=request_type or None,
            state=state,
            customer_phone=customer_phone or None,
            customer_email=customer_email or None,
        )

    @tool
    async def get_return_request_by_id(request_id: str) -> dict:
        """
        Fetch one return/exchange request by its request NUMBER - the code the
        customer quotes, like "RET777" - or by the partner's internal request id.

        Pass the value exactly as the customer gave it. A "RET..." number is
        looked up by request number; a 24-character hex id is looked up by id.

        If the live lookup is unavailable, the last state the partner sent us is
        returned with "is_cached": true and "last_updated_at". In that case tell
        the customer the status is as of that time and may have changed since.
        A response with "request": null means no such request exists - say that
        plainly instead of asking the customer to try again later.
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.aget_return_request_by_id(
            client_id=_client_id_from_state(state),
            request_id=request_id,
            state=state,
        )

    @tool
    @order_access_guarded(state, mutating=True)
    async def get_return_or_exchange_portal_link(
        order_number: str,
        customer_email: str = "",
        customer_phone: str = "",
        request_type: str = "",
        selected_line_items: list[dict] | None = None,
        return_reason: str = "",
        proof_provided: bool | None = None,
        tag_intact_confirmed: bool | None = None,
        desired_resolution: str = "",
        verification_identifier_type: str = "",
        verification_identifier_value: str = "",
    ) -> dict:
        """
        Use this when the customer wants to start a new return or exchange
        request for an order.
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.aget_return_or_exchange_portal(
            client_id=_client_id_from_state(state),
            order_number=order_number,
            customer_email=customer_email or None,
            request_type=request_type or None,
            state=state,
            customer_phone=customer_phone or None,
            selected_line_items=selected_line_items,
            return_reason=return_reason or None,
            proof_provided=proof_provided,
            tag_intact_confirmed=tag_intact_confirmed,
            desired_resolution=desired_resolution or None,
        )

    @tool
    @order_access_guarded(state)
    async def get_return_pickup_status(
        order_number: str,
        customer_phone: str = "",
        customer_email: str = "",
        verification_identifier_type: str = "",
        verification_identifier_value: str = "",
    ) -> dict:
        """
        Use this when the customer asks about return pickup, reverse pickup,
        return shipment, or return-to-origin status.
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.aget_return_pickup_status(
            client_id=_client_id_from_state(state),
            order_number=order_number,
            state=state,
            customer_phone=customer_phone or None,
            customer_email=customer_email or None,
        )

    @tool
    @order_access_guarded(state)
    async def get_refund_status_by_order_number(
        order_number: str,
        request_type: str = "return",
        customer_phone: str = "",
        customer_email: str = "",
        verification_identifier_type: str = "",
        verification_identifier_value: str = "",
    ) -> dict:
        """
        Use this when the customer asks whether refund has started, where the
        refund is, when money will arrive, refund SLA, wallet credit, or refund
        escalation for a return/exchange.
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.aget_refund_status(
            client_id=_client_id_from_state(state),
            order_number=order_number,
            request_type=request_type or None,
            state=state,
            customer_phone=customer_phone or None,
            customer_email=customer_email or None,
        )

    @tool
    @order_access_guarded(state, mutating=True)
    async def ensure_exchange_order_created(
        order_number: str,
        force: bool = False,
        customer_phone: str = "",
        customer_email: str = "",
        verification_identifier_type: str = "",
        verification_identifier_value: str = "",
    ) -> dict:
        """
        Use this when an exchange customer asks when the exchange order will be
        created or when configured policy says exchange order should be created.

        By default this will not create anything unless the client's
        return_exchange_automation config enables auto_create_exchange_order and
        the pickup/RTO status satisfies the policy. Use force only for internal
        testing or explicitly approved operational flows.
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.aensure_exchange_order(
            client_id=_client_id_from_state(state),
            order_number=order_number,
            state=state,
            force=force,
            customer_phone=customer_phone or None,
            customer_email=customer_email or None,
        )

    @tool
    @order_access_guarded(state, mutating=True)
    async def request_exchange_size_change(
        order_number: str,
        desired_size: str,
        customer_phone: str = "",
        customer_email: str = "",
        verification_identifier_type: str = "",
        verification_identifier_value: str = "",
    ) -> dict:
        """
        Use this when a customer with an existing exchange request wants a
        DIFFERENT size than what they originally selected (e.g. they picked
        the wrong size on the Return Prime portal and now want a different
        one).

        This does not change the size automatically — Return Prime does not
        provide an API to edit an existing request, and this store does not
        have exchange-order automation enabled. It captures the requested
        size, adds a note to the Shopify order, and raises an escalation so a
        human can make the correction in Return Prime's dashboard.
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.arequest_exchange_size_change(
            client_id=_client_id_from_state(state),
            order_number=order_number,
            desired_size=desired_size,
            state=state,
            customer_phone=customer_phone or None,
            customer_email=customer_email or None,
        )

    @tool
    @order_access_guarded(state)
    async def get_exchange_delivery_status(
        order_number: str,
        customer_phone: str = "",
        customer_email: str = "",
        verification_identifier_type: str = "",
        verification_identifier_value: str = "",
    ) -> dict:
        """
        Use this when the customer asks when the exchanged/replacement product
        will arrive, where the exchange delivery is, or the status of the
        forward shipment for an exchange item.

        This first checks the return/exchange partner to confirm whether an
        exchange order exists, then fetches the delivery status for that
        exchange order when available.
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.aget_exchange_delivery_status(
            client_id=_client_id_from_state(state),
            order_number=order_number,
            state=state,
            customer_phone=customer_phone or None,
            customer_email=customer_email or None,
        )

    @tool
    async def get_return_exchange_request_instructions(
        request_type: str = "",
    ) -> dict:
        """
        Use this when the customer asks how to raise a return, exchange, or
        refund request, especially when they are asking for the process/link
        rather than the status of an existing request.
        """
        from fashion_bot.core.orchestrator import ReturnExchangeOrchestrator

        return await ReturnExchangeOrchestrator.aget_return_exchange_instructions(
            client_id=_client_id_from_state(state),
            request_type=request_type or None,
            state=state,
        )

    return [
        get_return_status_by_order_number,
        list_return_requests_by_order_number,
        get_return_request_by_id,
        get_return_or_exchange_portal_link,
        get_return_pickup_status,
        get_refund_status_by_order_number,
        ensure_exchange_order_created,
        request_exchange_size_change,
        get_exchange_delivery_status,
        get_return_exchange_request_instructions,
    ]
