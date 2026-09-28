"""
Shopify Order Updation — shared data classes and Shiprocket order lookup.

ShippingAddress is used by ShopifyOrderAdapter.aupdate_order for address
parsing / validation / format conversion.

aget_shiprocket_complete_order_data is used by ShiprocketOrderAdapter for
fetching full Shiprocket order records.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ShippingAddress — used by ShopifyOrderAdapter.aupdate_order
# ---------------------------------------------------------------------------

@dataclass
class ShippingAddress:
    """Shipping address information."""

    first_name: str
    last_name: str
    address1: str
    address2: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    country: str = "India"

    def validate(self) -> Dict[str, Any]:
        errors = []
        if not self.first_name:
            errors.append("First name is required")
        if not self.last_name:
            errors.append("Last name is required")
        if not self.address1 or not self.address1.strip():
            errors.append("Address Line 1 is required")
        if not self.zip_code:
            errors.append("PIN/ZIP Code is required")
        if not self.phone:
            errors.append("Phone number is required")
        warnings: List[str] = []
        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    async def avalidate(self) -> Dict[str, Any]:
        """Async validation used by the async update flow."""
        errors = []

        if not self.first_name:
            errors.append("First name is required")
        if not self.last_name:
            errors.append("Last name is required")
        if not self.address1 or not self.address1.strip():
            errors.append("Address Line 1 is required")
        if not self.zip_code:
            errors.append("PIN/ZIP Code is required")
        if not self.phone:
            errors.append("Phone number is required")
        elif not self._is_valid_phone(self.phone):
            errors.append("Invalid phone number format")
        else:
            self.phone = self._normalize_phone(self.phone)

        warnings: List[str] = []
        pin_info = await self._alookup_pincode_online(self.zip_code) if self.zip_code else None
        if pin_info and pin_info.get("city") and pin_info.get("state"):
            city_match = self.city.strip().lower() == pin_info["city"].strip().lower()
            state_match = self.state.strip().lower() == pin_info["state"].strip().lower()
            if not (city_match and state_match):
                warnings.append(
                    f"Note: PIN {self.zip_code} typically corresponds to {pin_info['city']}, "
                    f"{pin_info['state']}, but you've provided {self.city}, {self.state}. "
                    "You can proceed with your preferred address."
                )

        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    @staticmethod
    def _is_valid_phone(phone: str) -> bool:
        import re

        cleaned_phone = phone.replace(" ", "").replace("-", "").strip()
        return re.match(r"^(\+91|91)?\d{10}$", cleaned_phone) is not None

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        import re

        cleaned = phone.replace(" ", "").replace("-", "").strip()
        match = re.match(r"^(\+91|91)?(\d{10})$", cleaned)
        if match:
            digits = match.group(2)
            return f"+91{digits}"
        return phone

    @staticmethod
    async def _alookup_pincode_online(pin_code: str) -> Optional[Dict[str, str]]:
        """Async PIN code lookup used by validation warnings."""
        try:
            url = f"https://api.postalpincode.in/pincode/{pin_code}"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                )
            }
            client = await get_shared_async_http_client()
            _t0 = time.monotonic()
            resp = await client.get(url, headers=headers, timeout=10)
            logger.info(f"[SHOPIFY] GET {url} elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={resp.status_code}")
            data = resp.json()

            if (
                isinstance(data, list)
                and data
                and data[0].get("Status") == "Success"
                and "PostOffice" in data[0]
                and data[0]["PostOffice"]
            ):
                po = data[0]["PostOffice"][0]
                return {
                    "city": po.get("District", ""),
                    "state": po.get("State", ""),
                }
        except Exception as exc:
            logger.debug("Exception during PIN lookup: %s", exc)
        return None

    def to_shopify_format(self) -> Dict[str, str]:
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "address1": self.address1,
            "address2": self.address2,
            "city": self.city.title(),
            "province": self.state,
            "zip": self.zip_code,
            "country": self.country,
            "phone": self.phone,
        }

    def to_shiprocket_format(self) -> Dict[str, Any]:
        return {
            "name": f"{self.first_name} {self.last_name}".strip(),
            "address1": self.address1,
            "address2": self.address2,
            "city": self.city,
            "state": self.state,
            "zip": self.zip_code,
            "phone": self.phone,
            "country": self.country,
        }


# ---------------------------------------------------------------------------
# Shiprocket order data lookup — used by ShiprocketOrderAdapter
# ---------------------------------------------------------------------------

def _build_state(client_id: Optional[str]) -> Dict[str, Any]:
    return {"client_id": client_id} if client_id else {}


@dataclass
class ShiprocketOrderData:
    """Complete Shiprocket order data structure."""

    order_id: str
    channel_order_id: str
    status: str
    order_data: Dict[str, Any]
    total_orders_found: int
    shipment_status: str

    @staticmethod
    def _categorize_shipment_status(status: str) -> str:
        status_upper = (status or "").upper()
        if status_upper in ["DELIVERED", "RTO DELIVERED", "RETURN DELIVERED"]:
            return "Completed Shipment"
        if status_upper == "CANCELED":
            return "Cancelled Shipment"
        return "Pending Shipment"


async def aget_shiprocket_complete_order_data(
    order_id: str,
    shiprocket_creds=None,
    client_id: Optional[str] = None,
) -> List[ShiprocketOrderData]:
    from fashion_bot.shiprocket.tools.logistics_adapter import ShiprocketLogisticsAdapter

    effective_client_id = client_id or (shiprocket_creds.client_id if shiprocket_creds else None)
    if not effective_client_id:
        raise ValueError("client_id is required to fetch Shiprocket order data")

    adapter = ShiprocketLogisticsAdapter(client_id=effective_client_id)
    state = _build_state(effective_client_id)
    orders = await adapter.aget_matching_orders(order_id, state=state)
    total_orders = len(orders)
    return [
        ShiprocketOrderData(
            order_id=str(order.get("order_id", "")),
            channel_order_id=str(order.get("channel_order_id", "")),
            status=str(order.get("status", "")),
            order_data=order.get("order_data", {}) or {},
            total_orders_found=total_orders,
            shipment_status=ShiprocketOrderData._categorize_shipment_status(order.get("status", "")),
        )
        for order in orders
    ]
