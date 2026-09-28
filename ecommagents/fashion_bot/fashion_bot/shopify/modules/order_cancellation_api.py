"""
Async-backed Shopify Order Cancellation compatibility layer.

This module keeps the older public API stable while routing I/O through the
native async order/logistics services.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


async def get_order_prefix(client_id: str = None) -> str:
    """Get order prefix from configuration."""
    try:
        from fashion_bot.config_manager import aget_config

        prefix = await aget_config("order_prefix", default=None, client_id=client_id)
        if prefix is None:
            logger.warning(
                "order_prefix NOT CONFIGURED for client %s - please add to client_configs table",
                client_id,
            )
            return ""
        return prefix.lower() if prefix else ""
    except Exception as exc:
        logger.warning("Failed to get order_prefix: %s", exc)
        return ""


class CancellationReason(Enum):
    """Valid cancellation reasons."""

    CUSTOMER = "customer"
    INVENTORY = "inventory"
    FRAUD = "fraud"
    DECLINED = "declined"
    OTHER = "other"


@dataclass
class OrderIdentifier:
    """Order identifier with flexible input formats."""

    value: str

    def validate(self) -> Dict[str, Any]:
        errors = []
        if not self.value:
            errors.append("Order identifier is required")
        return {"valid": len(errors) == 0, "errors": errors}

    async def to_gv_format(self, client_id: Optional[str] = None) -> str:
        order_prefix = await get_order_prefix(client_id=client_id)
        clean_name = str(self.value).lstrip("#").lower()
        if order_prefix and clean_name.startswith(order_prefix):
            clean_name = clean_name[len(order_prefix) :]
        return f"#{order_prefix}{clean_name}"


@dataclass
class CancellationRequest:
    """Cancellation request with all necessary information."""

    order_identifier: OrderIdentifier
    reason: CancellationReason
    send_email: bool = True
    restock_items: bool = True
    currency: str = "INR"

    def validate(self) -> Dict[str, Any]:
        errors = []
        id_validation = self.order_identifier.validate()
        if not id_validation["valid"]:
            errors.extend(id_validation["errors"])
        if not isinstance(self.reason, CancellationReason):
            errors.append("Invalid cancellation reason")
        return {"valid": len(errors) == 0, "errors": errors}


@dataclass
class ShopifyEnvironment:
    """Shopify API environment configuration."""

    access_token: str
    shop_url: str
    api_version: str = "2023-10"

    def validate(self) -> Dict[str, Any]:
        errors = []
        if not self.access_token:
            errors.append("Shopify access token is required")
        if not self.shop_url:
            errors.append("Shopify shop URL is required")
        return {"valid": len(errors) == 0, "errors": errors}


@dataclass
class ShiprocketCredentials:
    """Shiprocket API credentials."""

    client_id: Optional[str]
    email: Optional[str] = None
    password: Optional[str] = None
    base_url: str = "https://apiv2.shiprocket.in/v1/external"
    _config: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self._config is not None:
            # Pre-fetched config supplied (e.g. by acreate) — skip sync DB call.
            config = self._config
            self._config = None  # clear so it's not serialised
            if config:
                self.email = self.email or config.get("email")
                self.password = self.password or config.get("password")
                if config.get("api_base") and self.base_url == "https://apiv2.shiprocket.in/v1/external":
                    self.base_url = config.get("api_base")
            return

        # No sync DB fallback — use ShiprocketCredentials.acreate() for async config loading.

    @classmethod
    async def acreate(cls, client_id: Optional[str] = None, email: Optional[str] = None,
                      password: Optional[str] = None) -> "ShiprocketCredentials":
        """Async factory that fetches config without blocking the event loop."""
        config: Optional[Dict[str, Any]] = None
        if client_id and (not email or not password):
            try:
                from fashion_bot.config_manager import aget_shiprocket_config

                config = await aget_shiprocket_config(client_id=client_id)
            except Exception as exc:
                logger.warning(
                    "Failed to async-fetch Shiprocket credentials for client %s: %s",
                    client_id,
                    exc,
                )
        return cls(client_id=client_id, email=email, password=password, _config=config)

    def validate(self) -> Dict[str, Any]:
        errors = []
        if self.client_id and not self.email:
            errors.append(f"Shiprocket email is missing for client {self.client_id}")
        if self.client_id and not self.password:
            errors.append(f"Shiprocket password is missing for client {self.client_id}")
        return {"valid": len(errors) == 0, "errors": errors}


def _build_state(client_id: Optional[str]) -> Dict[str, Any]:
    return {"client_id": client_id} if client_id else {}


class OrderCancellationAPI:
    """Compatibility API for cancelling Shopify orders."""

    def __init__(
        self,
        shopify_env: ShopifyEnvironment,
        client_id: Optional[str],
        shiprocket_creds: Optional[ShiprocketCredentials] = None,
    ):
        self.shopify_env = shopify_env
        self.client_id = client_id
        self.shiprocket_creds = shiprocket_creds or ShiprocketCredentials(client_id=client_id)
        self._validate_environment()

    def _validate_environment(self) -> None:
        shopify_validation = self.shopify_env.validate()
        if not shopify_validation["valid"]:
            raise ValueError(f"Invalid Shopify environment: {shopify_validation['errors']}")

    async def _aresolve_order_id(self, order_identifier: OrderIdentifier) -> Dict[str, Any]:
        from fashion_bot.core.factory import ServiceFactory

        state = _build_state(self.client_id)
        order_service = await ServiceFactory.aget_order_service(client_id=self.client_id, state=state, vendor="shopify")
        order = await order_service.aget_order_details(order_identifier.value, state=state)
        if not order:
            return {
                "success": False,
                "error": "Order not found for cancellation",
                "details": {"identifier": order_identifier.value},
            }
        order_id = order.get("id")
        if not order_id:
            return {
                "success": False,
                "error": "Shopify order ID missing",
                "details": {"identifier": order_identifier.value},
            }
        return {"success": True, "order_id": str(order_id), "order_data": order}

    async def _aadd_note_to_order(self, order_identifier: OrderIdentifier, note: str) -> Dict[str, Any]:
        from fashion_bot.core.factory import ServiceFactory

        state = _build_state(self.client_id)
        order_service = await ServiceFactory.aget_order_service(client_id=self.client_id, state=state, vendor="shopify")
        return await order_service.aadd_order_note(order_identifier.value, note, state=state)

    async def _acancel_shiprocket_orders(self, order_identifier: OrderIdentifier) -> Dict[str, Any]:
        from fashion_bot.core.factory import ServiceFactory

        if not self.client_id:
            return {"success": True, "details": "No client_id available, skipping Shiprocket cancellation."}

        state = _build_state(self.client_id)
        logistics_service = await ServiceFactory.aget_logistics_service(client_id=self.client_id, state=state)
        result = await logistics_service.acancel_shipment(order_identifier.value, state=state)
        if result.get("success"):
            return result

        error_message = str(result.get("error") or "").lower()
        if "not found" in error_message or "missing" in error_message:
            return {
                "success": True,
                "details": "No Shiprocket orders found, skipping Shiprocket cancellation.",
                "skipped": result,
            }
        return result

    async def _acancel_in_shopify(
        self,
        order_identifier: OrderIdentifier,
        request: CancellationRequest,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        from fashion_bot.core.factory import ServiceFactory

        state = _build_state(self.client_id)
        order_service = await ServiceFactory.aget_order_service(client_id=self.client_id, state=state, vendor="shopify")
        return await order_service.acancel_order(
            order_identifier.value,
            reason=request.reason.value,
            state=state,
            skip_refund=skip_refund,
        )

    async def acancel_order(
        self,
        request: CancellationRequest,
        custom_note: Optional[str] = None,
        skip_refund: bool = False,
    ) -> Dict[str, Any]:
        validation_result = request.validate()
        if not validation_result["valid"]:
            return {
                "success": False,
                "error": "Validation failed",
                "details": validation_result["errors"],
            }

        order_id_result = await self._aresolve_order_id(request.order_identifier)
        if not order_id_result["success"]:
            return order_id_result

        if custom_note:
            note_text = f"Cancellation reason by customer : {custom_note}\n(Order cancelled by chatbot)"
        else:
            note_text = (
                f"Cancellation reason by customer : {request.reason.value}\n"
                "(Order cancelled by chatbot)"
            )

        note_result = await self._aadd_note_to_order(request.order_identifier, note_text)
        if not note_result.get("success"):
            return note_result

        shiprocket_result = await self._acancel_shiprocket_orders(request.order_identifier)
        shopify_result = await self._acancel_in_shopify(
            request.order_identifier,
            request,
            skip_refund=skip_refund,
        )

        error_msg = None
        if not shopify_result.get("success", False):
            error_msg = shopify_result.get("error")
        elif shiprocket_result.get("success") is False:
            error_msg = shiprocket_result.get("error")

        result = {
            "success": shopify_result.get("success", False),
            "shopify": shopify_result,
            "shiprocket": shiprocket_result,
        }
        if error_msg:
            result["error"] = error_msg
        return result

CANCELLATION_REASONS = [reason.value for reason in CancellationReason]


def get_valid_cancellation_reason(reason: str) -> Tuple[str, Optional[str]]:
    if reason and reason.lower() in CANCELLATION_REASONS:
        return reason.lower(), None
    return "other", f"Cancellation reason provided by user: {reason}"


def get_allowed_cancellation_reasons() -> list:
    return CANCELLATION_REASONS


async def acancel_order_from_data(
    order_identifier: str,
    reason: str,
    shopify_env: Dict[str, str],
    client_id: Optional[str] = None,
    shiprocket_creds: Optional[Dict[str, str]] = None,
    custom_note: Optional[str] = None,
    skip_refund: bool = False,
) -> Dict[str, Any]:
    order_id = OrderIdentifier(value=order_identifier)

    try:
        cancellation_reason = CancellationReason(reason.lower())
    except ValueError:
        return {
            "success": False,
            "error": (
                f"Invalid cancellation reason: {reason}. "
                f"Valid reasons: {[r.value for r in CancellationReason]}"
            ),
        }

    request = CancellationRequest(order_identifier=order_id, reason=cancellation_reason)
    env = ShopifyEnvironment(
        access_token=shopify_env["access_token"],
        shop_url=shopify_env["shop_url"],
        api_version=shopify_env.get("api_version", "2023-10"),
    )

    shiprocket = None
    if shiprocket_creds:
        shiprocket = await ShiprocketCredentials.acreate(
            client_id=client_id,
            email=shiprocket_creds.get("email"),
            password=shiprocket_creds.get("password"),
        )

    api = OrderCancellationAPI(env, client_id, shiprocket)
    return await api.acancel_order(request, custom_note=custom_note, skip_refund=skip_refund)
