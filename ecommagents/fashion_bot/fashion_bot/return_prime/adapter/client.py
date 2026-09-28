"""Return Prime transport adapter.

This adapter intentionally exposes only the confirmed Return Prime request-read
and webhook-management APIs. Business logic such as order normalization and
Shopify customer resolution belongs in the workflow layer.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from fashion_bot.env_loader import get_env
from fashion_bot.utils.http_client import get_shared_async_http_client

RETURN_PRIME_REQUEST_BASE = "https://admin.returnprime.com"
RETURN_PRIME_WEBHOOK_BASE = "http://api.returnprime.co/v2/webhook/"
RETURN_PRIME_PORTAL_PATH = "/external/fetch-order"

logger = logging.getLogger(__name__)


async def _aget_json_config_safe(config_key: str, client_id: str) -> dict:
    try:
        from fashion_bot.config_manager import aget_json_config

        return await aget_json_config(config_key, client_id=client_id) or {}
    except Exception:
        return {}


class ReturnPrimeAdapter:
    """Async transport adapter for confirmed Return Prime APIs."""

    def build_return_portal_link(
        self,
        *,
        order_number: str,
        email: str,
        store: str,
        channel_id: str,
    ) -> str:
        query = urlencode(
            {
                "order_number": order_number,
                "email": email,
                "store": store,
                "channel_id": channel_id,
            }
        )
        return f"{RETURN_PRIME_REQUEST_BASE}{RETURN_PRIME_PORTAL_PATH}?{query}"

    async def _config(self, client_id: str) -> dict:
        details = await _aget_json_config_safe("return_prime_details", client_id)
        request_base_url = (
            details.get("api_base")
            or details.get("base_url")
            or get_env("RETURN_PRIME_API_BASE", RETURN_PRIME_REQUEST_BASE)
            or RETURN_PRIME_REQUEST_BASE
        )
        webhook_base_url = (
            details.get("webhook_base_url")
            or details.get("webhook_api_base")
            or get_env("RETURN_PRIME_WEBHOOK_API_BASE", RETURN_PRIME_WEBHOOK_BASE)
            or RETURN_PRIME_WEBHOOK_BASE
        )
        token = (
            details.get("x_rp_token")
            or details.get("api_token")
            or get_env("RETURN_PRIME_X_RP_TOKEN")
            or get_env("RETURN_PRIME_API_TOKEN")
        )
        return {
            "request_base_url": str(request_base_url).rstrip("/"),
            "webhook_base_url": str(webhook_base_url).rstrip("/") + "/",
            "x_rp_token": token,
        }

    def _headers(self, config: dict, *, include_content_type: bool = False) -> dict:
        headers = {
            "Accept": "application/json",
            "x-rp-token": config["x_rp_token"],
        }
        if include_content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _normalize_result(
        self,
        *,
        success: bool,
        status_code: int,
        raw: Any,
        data: Any = None,
        error: dict | None = None,
    ) -> dict:
        source = raw if isinstance(raw, dict) else {}
        message_code = None
        message = None
        for key in ("messageCode", "message_code", "code", "status_code_name"):
            value = source.get(key)
            if value not in (None, ""):
                message_code = str(value)
                break
        for key in ("message", "detail", "error"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                message = value
                break
        return {
            "success": success,
            "status_code": status_code,
            "message_code": message_code,
            "message": message,
            "data": data,
            "error": error,
            "raw": raw,
        }

    async def _send(
        self,
        client_id: str,
        *,
        method: str,
        url: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        config = await self._config(client_id)
        if not config.get("x_rp_token"):
            return self._normalize_result(
                success=False,
                status_code=500,
                raw=None,
                error={
                    "code": "RETURN_PRIME_CONFIG_MISSING",
                    "message": "Missing required Return Prime configuration.",
                    "raw": None,
                },
            )

        clean_params = {
            key: value for key, value in (params or {}).items() if value not in (None, "")
        } or None
        clean_json = {
            key: value for key, value in (json_body or {}).items() if value not in (None, "")
        } or None

        try:
            http = await get_shared_async_http_client()
            logger.info(
                "[RETURN_PRIME_API] request client_id=%s method=%s url=%s params=%s",
                client_id,
                method.upper(),
                url,
                clean_params or {},
            )
            response = await http.request(
                method=method.upper(),
                url=url,
                params=clean_params,
                json=clean_json,
                headers=self._headers(config, include_content_type=clean_json is not None),
                timeout=20,
            )
        except httpx.RequestError as exc:
            logger.warning(
                "[RETURN_PRIME_API] request_failed client_id=%s method=%s url=%s error_type=%s error=%r",
                client_id,
                method.upper(),
                url,
                type(exc).__name__,
                exc,
            )
            return self._normalize_result(
                success=False,
                status_code=0,
                raw=None,
                error={
                    "code": "RETURN_PRIME_REQUEST_FAILED",
                    "message": str(exc),
                    "raw": None,
                },
            )

        try:
            payload = response.json()
        except ValueError:
            payload = {"raw_text": response.text}

        api_success = not (isinstance(payload, dict) and payload.get("status") is False)
        logger.info(
            "[RETURN_PRIME_API] response client_id=%s method=%s url=%s status_code=%s api_success=%s message_code=%s",
            client_id,
            method.upper(),
            url,
            response.status_code,
            api_success,
            payload.get("messageCode") if isinstance(payload, dict) else None,
        )
        if response.is_success and api_success:
            return self._normalize_result(
                success=True,
                status_code=response.status_code,
                raw=payload,
                data=payload,
            )

        return self._normalize_result(
            success=False,
            status_code=response.status_code,
            raw=payload,
            data=payload if response.is_success else None,
            error={
                "code": payload.get("code") if isinstance(payload, dict) else None,
                "message": (
                    payload.get("message")
                    if isinstance(payload, dict)
                    else f"Return Prime request failed with status {response.status_code}."
                ),
                "raw": payload,
            },
        )

    async def list_requests(
        self,
        client_id: str,
        *,
        order_name: str | None = None,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        request_number: str | None = None,
        request_type: str | None = None,
        awb: str | None = None,
        order_id: str | None = None,
        created_at_min: str | None = None,
        created_at_max: str | None = None,
    ) -> dict:
        config = await self._config(client_id)
        return await self._send(
            client_id,
            method="GET",
            url=f"{config['request_base_url']}/return-exchange/v2",
            params={
                "order_name": order_name,
                "customer_email": customer_email,
                "customer_phone": customer_phone,
                "request_number": request_number,
                "request_type": request_type,
                "awb": awb,
                "order_id": order_id,
                "created_at_min": created_at_min,
                "created_at_max": created_at_max,
            },
        )

    async def get_request_by_id(self, client_id: str, request_id: str) -> dict:
        config = await self._config(client_id)
        return await self._send(
            client_id,
            method="GET",
            url=f"{config['request_base_url']}/return-exchange/v2/{request_id}",
        )

    async def register_webhook(self, client_id: str, url: str, topics: list[str]) -> dict:
        config = await self._config(client_id)
        return await self._send(
            client_id,
            method="POST",
            url=config["webhook_base_url"],
            json_body={"url": url, "topics": topics},
        )

    async def list_webhooks(self, client_id: str) -> dict:
        config = await self._config(client_id)
        return await self._send(
            client_id,
            method="GET",
            url=config["webhook_base_url"],
        )

    async def update_webhook(
        self,
        client_id: str,
        webhook_id: str,
        url: str,
        topics: list[str],
    ) -> dict:
        config = await self._config(client_id)
        return await self._send(
            client_id,
            method="PUT",
            url=f"{config['webhook_base_url'].rstrip('/')}/{webhook_id}",
            json_body={"url": url, "topics": topics},
        )

    async def delete_webhook(self, client_id: str, webhook_id: str) -> dict:
        config = await self._config(client_id)
        webhook_url = config["webhook_base_url"]
        if webhook_id:
            webhook_url = f"{webhook_url.rstrip('/')}/{webhook_id}"
        return await self._send(
            client_id,
            method="DELETE",
            url=webhook_url,
        )


return_prime_service = ReturnPrimeAdapter()
ReturnPrimeService = ReturnPrimeAdapter
