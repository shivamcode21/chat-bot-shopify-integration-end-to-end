"""Interfaces for return/exchange partners."""

from __future__ import annotations

from typing import Protocol


class ReturnPartnerService(Protocol):
    """Stateless service interface implemented by return/exchange partners."""

    partner_name: str

    async def get_status_by_order_number(
        self,
        client_id: str,
        order_number: str,
        *,
        request_type: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        """Return normalized return/exchange status for an order."""

    async def list_requests_by_order_number(
        self,
        client_id: str,
        order_number: str,
        *,
        request_type: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
    ) -> dict:
        """Return normalized return/exchange requests for an order."""

    async def get_request_by_id(self, client_id: str, request_id: str) -> dict:
        """Return normalized return/exchange request details."""

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
        """Return a partner portal link or create a native return/exchange."""

    async def normalize_webhook(self, client_id: str, payload: dict, headers: dict) -> dict:
        """Normalize a partner webhook payload without performing side effects."""
