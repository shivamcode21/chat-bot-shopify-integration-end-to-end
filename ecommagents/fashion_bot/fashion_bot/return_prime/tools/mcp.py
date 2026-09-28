"""Return Prime MCP tools."""

from __future__ import annotations

from fashion_bot.tools import mcp
from fashion_bot.return_prime.workflow.constants import NO_REQUEST_MESSAGE
from fashion_bot.return_prime.workflow.service import return_prime_workflow
from fashion_bot.client_context import get_client_id


@mcp.tool()
async def get_return_prime_status_by_order_number(
    order_number: str,
    customer_email: str = "",
    customer_phone: str = "",
    request_type: str = "",
) -> dict:
    """Get a safe Return Prime status summary by customer-facing order number."""
    client_id = get_client_id()
    return await return_prime_workflow.get_status_by_order_number(
        client_id,
        order_number,
        customer_email=customer_email or None,
        customer_phone=customer_phone or None,
        request_type=request_type or None,
    )


@mcp.tool()
async def list_return_prime_requests_by_order_number(
    order_number: str,
    customer_email: str = "",
    customer_phone: str = "",
    request_type: str = "",
) -> dict:
    """List Return Prime requests for a customer-facing order number."""
    client_id = get_client_id()
    result = await return_prime_workflow.list_requests_by_order_number(
        client_id,
        order_number,
        customer_email=customer_email or None,
        customer_phone=customer_phone or None,
        request_type=request_type or None,
    )
    if not result.get("success"):
        return result
    requests = result.get("requests", [])
    if not requests:
        return {
            "success": True,
            "status_code": 200,
            "order_name": result.get("order_name"),
            "message": NO_REQUEST_MESSAGE,
            "requests": [],
        }
    return {
        "success": True,
        "status_code": 200,
        "order_name": result.get("order_name"),
        "request_count": len(requests),
        "requests": requests,
    }


@mcp.tool()
async def get_return_prime_request_by_id(request_id: str) -> dict:
    """Fetch one exact Return Prime request by request id."""
    client_id = get_client_id()
    return await return_prime_workflow.get_request_by_id(client_id, request_id)


@mcp.tool()
async def get_return_prime_portal_link(
    order_number: str,
    customer_email: str = "",
) -> dict:
    """Build a Return Prime portal link to share with a customer for returns or exchanges."""
    client_id = get_client_id()
    result = await return_prime_workflow.get_return_portal_link(
        client_id,
        order_number,
        customer_email=customer_email or None,
    )
    return {
        "success": result.get("success", False),
        "status_code": result.get("status_code", 500),
        "order_name": result.get("order_name"),
        "customer_email": result.get("customer_email"),
        "portal_url": result.get("portal_url"),
        "message": result.get("message"),
        "shopify_lookup_used": result.get("shopify_lookup_used", False),
    }
