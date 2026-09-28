"""
Modular API for Shopify Order Creation
Provides complete workflows for creating orders with proper validation and error handling.
"""

import asyncio
import time
from typing import Dict, Any, Optional, List, Union
import re
import json
import os
import random
import string
from dataclasses import dataclass
import httpx

import logging
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.shopify.order_tags import OrderTag

logger = logging.getLogger(__name__)

# Import config manager for dynamic discount fetching
try:
    from fashion_bot.config_manager import aget_primary_discount_coupon
except ImportError:
    async def aget_primary_discount_coupon():
        return None

@dataclass
class CustomerInfo:
    """Customer information for order creation."""
    email: str
    phone: str
    
    def validate(self) -> Dict[str, Any]:
        """Validate customer information."""
        errors = []
        
        if not self.email:
            # Generate dummy email if not provided
            if self.phone and self.phone.isdigit():
                unique_part = self.phone
            else:
                unique_part = ''.join(random.choices(string.digits, k=10))
            self.email = f"dummy{unique_part}@ecommbot.com"
        
        if not self._is_valid_email(self.email):
            errors.append("Invalid email address format")
        
        if not self._is_valid_phone(self.phone):
            errors.append("Invalid phone number format")
        
        return {"valid": len(errors) == 0, "errors": errors}
    
    @staticmethod
    def _is_valid_email(email: str) -> bool:
        """Validate email address format."""
        return re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email) is not None
    
    @staticmethod
    def _is_valid_phone(phone: str) -> bool:
        """Validate Indian phone number (10 digits, optional +91/91 or 0 prefix).

        Tolerant of spaces, dashes and dots in the input so a number like
        "+91 8075053289" validates instead of being rejected on the separator.
        """
        if not phone:
            return False
        cleaned = re.sub(r"[\s\-\.]", "", str(phone))
        return re.match(r"^(?:\+?91|0)?\d{10}$", cleaned) is not None

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
        """Validate shipping address."""
        errors = []
        
        if not self.first_name:
            errors.append("First name is required")
        
        if not self.last_name:
            errors.append("Last name is required")
        
        if not self.address1:
            errors.append("Address Line 1 is required")
        elif len(self.address1) < 1:
            errors.append("Address must be at least 1 character")
        # Note: Removed requirement for both letters AND numbers in address1
        # Many valid addresses (especially in India) use landmark-based descriptions
        # without house numbers (e.g., "Near Bus Stand, Main Road, Village Name")
        
        if not self.zip_code:
            errors.append("PIN/ZIP Code is required")
        
        if not self.phone:
            errors.append("Phone number is required")
        elif not CustomerInfo._is_valid_phone(self.phone):
            errors.append("Invalid phone number format")
        
        # PIN code validation removed - allowing any city/state combination
        # This was causing issues with valid cities not matching expected PIN code data
        
        return {"valid": len(errors) == 0, "errors": errors}
    
    @staticmethod
    async def _alookup_pincode_online(pin_code: str) -> Optional[Dict[str, str]]:
        """Async lookup PIN code information from online API."""
        try:
            url = f"https://api.postalpincode.in/pincode/{pin_code}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            }
            client = await get_shared_async_http_client()
            resp = await client.get(url, headers=headers, timeout=10)
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
                    "state": po.get("State", "")
                }
        except Exception as e:
            logger.debug(f"[PIN_LOOKUP] Exception: {e}")
        return None
    
    def _format_phone_with_country_code(self, phone: str) -> str:
        """Format phone number with +91 country code for Shopify."""
        if not phone:
            return ""
        
        # Remove any spaces, dashes, or other non-digit characters except +
        cleaned = ''.join(c for c in phone if c.isdigit() or c == '+')
        
        # If already has + prefix, return as is
        if cleaned.startswith('+'):
            return cleaned
        
        # Remove 91 prefix if present (for numbers like 919012345678)
        if cleaned.startswith('91') and len(cleaned) == 12:
            cleaned = cleaned[2:]
        
        # Add +91 prefix for 10-digit Indian numbers
        if len(cleaned) == 10:
            return f"+91{cleaned}"
        
        # Return as-is if not a standard format
        return cleaned
    
    def to_shopify_format(self) -> Dict[str, str]:
        """Convert to Shopify shipping address format."""
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "address1": self.address1,
            "address2": self.address2,
            "city": self.city.title() if self.city else "",
            "province": self.state,
            "zip": self.zip_code,
            "country": self.country,
            "phone": self._format_phone_with_country_code(self.phone)
        }

@dataclass
class ProductSelection:
    """Product selection information using direct product ID."""
    product_id: Union[str, int]  # Product ID (always numeric)
    variant_id: Union[str, int]  # Variant ID
    quantity: int
    
    def validate(self) -> Dict[str, Any]:
        """Validate product selection."""
        errors = []
        
        if not self.product_id:
            errors.append("Product ID is required")
        
        if not self.variant_id:
            errors.append("Variant ID is required")
        
        if not isinstance(self.quantity, int) or self.quantity <= 0:
            errors.append("Quantity must be a positive integer")
        
        return {"valid": len(errors) == 0, "errors": errors}

@dataclass
class ShopifyEnvironment:
    """Shopify API environment configuration."""
    access_token: str
    shop_url: str
    api_version: str = "2025-07"
    
    def validate(self) -> Dict[str, Any]:
        """Validate Shopify environment."""
        errors = []
        
        if not self.access_token:
            errors.append("Shopify access token is required")
        
        if not self.shop_url:
            errors.append("Shopify shop URL is required")
        
        return {"valid": len(errors) == 0, "errors": errors}

class OrderCreationAPI:
    """Modular API for creating Shopify orders."""
    
    def __init__(self, shopify_env: ShopifyEnvironment):
        """
        Initialize the Order Creation API.
        
        Args:
            shopify_env: Shopify environment configuration
        """
        self.shopify_env = shopify_env
        self._validate_environment()
    
    def _validate_environment(self) -> None:
        """Validate Shopify environment on initialization."""
        validation = self.shopify_env.validate()
        if not validation["valid"]:
            raise ValueError(f"Invalid Shopify environment: {validation['errors']}")
    
    def _format_phone_for_order(self, phone: str) -> str:
        """Format phone number with +91 country code for Shopify order."""
        if not phone:
            return ""
        
        # Remove any spaces, dashes, or other non-digit characters except +
        cleaned = ''.join(c for c in phone if c.isdigit() or c == '+')
        
        # If already has + prefix, return as is
        if cleaned.startswith('+'):
            return cleaned
        
        # Remove 91 prefix if present (for numbers like 919012345678)
        if cleaned.startswith('91') and len(cleaned) == 12:
            cleaned = cleaned[2:]
        
        # Add +91 prefix for 10-digit Indian numbers
        if len(cleaned) == 10:
            return f"+91{cleaned}"
        
        # Return as-is if not a standard format
        return cleaned
    
    async def acreate_order(
        self,
        customer: CustomerInfo,
        address: ShippingAddress,
        product: ProductSelection,
        discount_code: str = None,
        financial_status: str = None,
        payment_gateway_names: list = None,
        note: str = None,
        transactions: list = None
    ) -> Dict[str, Any]:
        validation_result = self._validate_inputs(customer, address, product)
        if not validation_result["valid"]:
            return {
                "success": False,
                "error": "Validation failed",
                "details": validation_result["errors"]
            }

        customer_id = None
        customer_id = await self._afind_or_create_customer(customer, address)
        product_result = await self._afetch_product_and_validate_variant(product)
        if not product_result["success"]:
            return product_result

        order_data = await self._assemble_order_data(
            customer,
            address,
            product_result["product"],
            product_result["variant"],
            product.quantity,
            discount_code,
            customer_id=customer_id,
            financial_status=financial_status,
            payment_gateway_names=payment_gateway_names,
            note=note,
            transactions=transactions
        )
        return await self._acreate_shopify_order(order_data)
    
    async def _afind_or_create_customer(
        self,
        customer: CustomerInfo,
        address: ShippingAddress
    ) -> Optional[int]:
        """
        Async variant of find/create customer.
        Returns the Shopify customer ID if found/created, None otherwise.
        """
        try:
            from fashion_bot.shopify.modules.customer_apis import asearch_customer_by_phone

            shopify_config = {
                "access_token": self.shopify_env.access_token,
                "shop_url": self.shopify_env.shop_url,
                "api_version": self.shopify_env.api_version,
            }

            existing_customer = await asearch_customer_by_phone(customer.phone, shopify_config)
            if existing_customer and existing_customer.get("id"):
                customer_id = existing_customer.get("id")
                if isinstance(customer_id, str) and "gid://shopify/Customer/" in customer_id:
                    customer_id = int(customer_id.replace("gid://shopify/Customer/", ""))
                logger.debug(f"[CUSTOMER] Found existing ID: {customer_id}")
                return customer_id

            new_customer_id = await self._acreate_customer(customer, address)
            if new_customer_id:
                logger.debug(f"[CUSTOMER] Created new ID: {new_customer_id}")
                return new_customer_id

            logger.debug("[CUSTOMER] Could not find or create, proceeding without customer_id")
            return None
        except Exception as e:
            logger.warning(f"[CUSTOMER] Error finding/creating: {e}")
            return None

    async def _acreate_customer(
        self,
        customer: CustomerInfo,
        address: ShippingAddress
    ) -> Optional[int]:
        """Create a new customer in Shopify asynchronously."""
        try:
            url = f"https://{self.shopify_env.shop_url}/admin/api/{self.shopify_env.api_version}/customers.json"
            headers = {
                "X-Shopify-Access-Token": self.shopify_env.access_token,
                "Content-Type": "application/json"
            }

            customer_data = {
                "customer": {
                    "first_name": address.first_name,
                    "last_name": address.last_name,
                    "email": customer.email,
                    "phone": customer.phone if customer.phone.startswith("+") else f"+91{customer.phone}",
                    "addresses": [{
                        "first_name": address.first_name,
                        "last_name": address.last_name,
                        "address1": address.address1,
                        "address2": address.address2,
                        "city": address.city,
                        "province": address.state,
                        "zip": address.zip_code,
                        "country": address.country,
                        "phone": customer.phone if customer.phone.startswith("+") else f"+91{customer.phone}"
                    }]
                }
            }

            client = await get_shared_async_http_client()
            _t0 = time.monotonic()
            response = await client.post(url, json=customer_data, headers=headers, timeout=15)
            _elapsed = int((time.monotonic() - _t0) * 1000)

            if response.status_code == 201:
                result = response.json()
                logger.info(f"[SHOPIFY] POST create_customer elapsed_ms={_elapsed} status=201")
                return result.get("customer", {}).get("id")

            logger.warning(f"[SHOPIFY] POST create_customer elapsed_ms={_elapsed} status={response.status_code}")
            return None
        except Exception as e:
            logger.warning(f"[SHOPIFY] Error creating customer: {e}")
            return None
    
    def _validate_inputs(
        self,
        customer: CustomerInfo,
        address: ShippingAddress,
        product: ProductSelection
    ) -> Dict[str, Any]:
        """Validate all input parameters."""
        all_errors = []
        
        # Validate customer
        customer_validation = customer.validate()
        if not customer_validation["valid"]:
            all_errors.extend(customer_validation["errors"])
        
        # Validate address
        address_validation = address.validate()
        if not address_validation["valid"]:
            all_errors.extend(address_validation["errors"])
        
        # Validate product
        product_validation = product.validate()
        if not product_validation["valid"]:
            all_errors.extend(product_validation["errors"])
        
        return {"valid": len(all_errors) == 0, "errors": all_errors}
    
    async def _afetch_product_and_validate_variant(self, product: ProductSelection) -> Dict[str, Any]:
        product_data = await self._afetch_product(product.product_id)
        if not product_data["success"]:
            return product_data
        return self._validate_variant(product_data["product"], product.variant_id)

    async def _afetch_product(self, product_id: Union[str, int]) -> Dict[str, Any]:
        url = f"https://{self.shopify_env.shop_url}/admin/api/{self.shopify_env.api_version}/products/{product_id}.json"
        headers = {
            "X-Shopify-Access-Token": self.shopify_env.access_token,
            "Content-Type": "application/json"
        }

        try:
            client = await get_shared_async_http_client()
            _t0 = time.monotonic()
            response = await client.get(url, headers=headers, timeout=15)
            logger.info(f"[SHOPIFY] GET product/{product_id} elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")

            if response.status_code == 200:
                data = response.json()
                if "product" in data:
                    return {"success": True, "product": data["product"]}
                return {"success": False, "error": f"Product with ID {product_id} not found"}

            if response.status_code == 404:
                return {"success": False, "error": f"Product with ID {product_id} not found"}

            try:
                error_json = response.json()
            except Exception:
                error_json = {}
            return {
                "success": False,
                "error": f"Shopify API error while fetching product: {response.status_code}",
                "details": error_json or response.text
            }
        except httpx.TimeoutException:
            return {"success": False, "error": "Request timed out while fetching product"}
        except httpx.HTTPError as e:
            return {"success": False, "error": f"Network connection error while fetching product: {str(e)}"}
        except Exception as e:
            return {"success": False, "error": f"Unexpected error while fetching product: {str(e)}"}
    
    @staticmethod
    def _rest_variant_in_stock(variant: Dict[str, Any]) -> bool:
        """Whether a REST Admin API variant is sellable.

        REST variant objects expose ``inventory_management``,
        ``inventory_policy`` and ``inventory_quantity`` (not an ``available``
        flag). A variant is sellable when:
          - inventory is not tracked (``inventory_management`` is falsy) — Shopify
            always allows the sale; or
          - the inventory policy is ``continue`` (oversell allowed); or
          - the tracked quantity is greater than zero.
        Fail-open on malformed/missing quantity so we never block a legitimate
        order on bad data.
        """
        if not variant.get("inventory_management"):
            return True
        if str(variant.get("inventory_policy", "")).lower() == "continue":
            return True
        try:
            return int(variant.get("inventory_quantity", 0)) > 0
        except (TypeError, ValueError):
            return True

    def _validate_variant(self, product_data: Dict[str, Any], variant_id: Union[str, int]) -> Dict[str, Any]:
        """Validate that variant ID belongs to the product and return variant info."""
        variants = product_data.get("variants", [])
        
        if not variants:
            return {
                "success": False,
                "error": "Product has no variants available",
                "details": {
                    "product_id": product_data.get("id"),
                    "product_title": product_data.get("title"),
                    "available_variants": []
                }
            }
        
        # Convert variant_id to string for comparison
        variant_id_str = str(variant_id)
        
        # Find the matching variant
        selected_variant = None
        for variant in variants:
            if str(variant.get("id")) == variant_id_str:
                selected_variant = variant
                break
        
        if selected_variant:
            # Check if variant is available for sale.
            # NOTE: the Shopify REST product/variant payload has no `available`
            # boolean — it exposes inventory_quantity / inventory_policy /
            # inventory_management — so the previous `.get("available", True)`
            # always passed and let out-of-stock orders through. Derive real
            # availability from the inventory fields instead.
            if not self._rest_variant_in_stock(selected_variant):
                return {
                    "success": False,
                    "error": "Selected variant is not available for sale",
                    "details": {
                        "product_id": product_data.get("id"),
                        "product_title": product_data.get("title"),
                        "variant_id": variant_id,
                        "variant_title": selected_variant.get("title"),
                        "available_variants": [
                            {
                                "id": v.get("id"),
                                "title": v.get("title"),
                                "price": v.get("price"),
                                "available": True,
                                "inventory_quantity": v.get("inventory_quantity", 0)
                            }
                            for v in variants if self._rest_variant_in_stock(v)
                        ]
                    }
                }
            
            return {
                "success": True,
                "product": product_data,
                "variant": selected_variant
            }
        else:
            # Variant not found, provide helpful information
            return {
                "success": False,
                "error": f"Variant ID {variant_id} does not belong to this product",
                "details": {
                    "product_id": product_data.get("id"),
                    "product_title": product_data.get("title"),
                    "requested_variant_id": variant_id,
                    "available_variants": [
                        {
                            "id": v.get("id"),
                            "title": v.get("title"),
                            "price": v.get("price"),
                            "available": self._rest_variant_in_stock(v),
                            "inventory_quantity": v.get("inventory_quantity", 0)
                        }
                        for v in variants
                    ]
                }
            }
    
    async def _assemble_order_data(
        self,
        customer: CustomerInfo,
        address: ShippingAddress,
        product: Dict[str, Any],
        variant: Dict[str, Any],
        quantity: int,
        discount_code: str = None,
        customer_id: Optional[int] = None,
        financial_status: str = None,
        payment_gateway_names: list = None,
        note: str = None,
        transactions: list = None
    ) -> Dict[str, Any]:
        """Assemble Shopify order data.
        
        Args:
            financial_status: Optional. "pending" for COD, "paid" for prepaid, "partially_paid" for partial payment. Defaults to "pending".
            payment_gateway_names: Optional. Payment gateway names. Defaults to ["Cash on Delivery (COD)"].
            note: Optional. Order note to attach (e.g., product change context).
            transactions: Optional. List of transaction dicts for partially_paid orders.
                          Example: [{"kind": "sale", "status": "success", "amount": 15.29}]
        """
        # Calculate price (apply discount if provided)
        original_price = float(variant.get("price", 0))
        final_price = original_price
        discount_amount = 0
        
        if discount_code:
            # Fetch discount details from database (async)
            db_discount_code, db_discount_fraction = await aget_discount_details()
            
            if db_discount_code and db_discount_fraction and discount_code.upper() == db_discount_code.upper():
                discount_amount = original_price * db_discount_fraction
                final_price = original_price - discount_amount
                logger.debug(f"[DISCOUNT] {original_price:.0f} - {discount_amount:.0f} ({db_discount_fraction*100}%) = {final_price:.0f} INR")
            else:
                logger.debug(f"[DISCOUNT] Code '{discount_code}' not recognized")

        line_item = {
            "title": product.get("title", "Unknown"),
            "price": final_price,  # Use discounted price
            "quantity": quantity,
            "variant_id": variant["id"]
        }
        
        # Get address in Shopify format (includes name and phone with country code)
        shipping_address = address.to_shopify_format()
        
        # Format phone with country code for order level using same logic as address
        formatted_phone = self._format_phone_for_order(customer.phone)
        
        logger.debug(f"[ORDER] customer_id={customer_id} city={shipping_address.get('city')}")
        
        # Use provided financial_status or default to COD
        effective_financial_status = financial_status or "pending"
        effective_payment_gateways = payment_gateway_names or ["Cash on Delivery (COD)"]
        
        order_data = {
            "line_items": [line_item],
            "currency": "INR",
            "send_receipt": False,
            "send_fulfillment_receipt": False,
            "financial_status": effective_financial_status,
            "payment_gateway_names": effective_payment_gateways,
            "shipping_address": shipping_address,
            "billing_address": shipping_address,
            "tags": OrderTag.BLOOMERCE_CREATED
        }
        
        # Add order note if provided
        if note:
            order_data["note"] = note
        
        # Add customer ID if we found/created one - this properly links the order to the customer
        # When using customer_id, we should NOT pass email/phone separately as Shopify uses the customer's data
        if customer_id:
            order_data["customer"] = {"id": customer_id}
        else:
            # Only add email/phone if we don't have a customer_id
            # This allows Shopify to auto-link to existing customer by email while using our shipping_address
            order_data["email"] = customer.email
            order_data["phone"] = formatted_phone
        
        # Add discount tracking information
        if discount_code:
            order_data["discount_codes"] = [{"code": discount_code}]
            if discount_amount > 0:
                # Add to tags for tracking
                order_data["tags"] += f",DISCOUNT_APPLIED:{discount_code}:{discount_amount:.2f}"
        
        # Add transactions for partially_paid orders (e.g., product change with differential)
        if transactions:
            order_data["transactions"] = transactions

        return order_data

    async def acreate_order_multi(
        self,
        customer: CustomerInfo,
        address: ShippingAddress,
        products: List[ProductSelection],
        discount_code: str = None,
        financial_status: str = None,
        payment_gateway_names: list = None,
        note: str = None,
        transactions: list = None
    ) -> Dict[str, Any]:
        """Create a SINGLE Shopify order containing multiple line items.

        Mirrors ``acreate_order`` but accepts a list of ``ProductSelection`` so a
        multi-item cart becomes one order instead of one order per item. Each
        product's variant is validated before the order is assembled; if any
        product fails validation the whole order is aborted (no partial orders).
        """
        if not products:
            return {"success": False, "error": "No products provided"}

        # Validate shared customer/address and every product up front.
        for product in products:
            validation_result = self._validate_inputs(customer, address, product)
            if not validation_result["valid"]:
                return {
                    "success": False,
                    "error": "Validation failed",
                    "details": validation_result["errors"]
                }

        customer_id = await self._afind_or_create_customer(customer, address)

        resolved_items = []
        for product in products:
            product_result = await self._afetch_product_and_validate_variant(product)
            if not product_result["success"]:
                return product_result
            resolved_items.append(
                (product_result["product"], product_result["variant"], product.quantity)
            )

        order_data = await self._assemble_order_data_multi(
            customer,
            address,
            resolved_items,
            discount_code,
            customer_id=customer_id,
            financial_status=financial_status,
            payment_gateway_names=payment_gateway_names,
            note=note,
            transactions=transactions,
        )
        return await self._acreate_shopify_order(order_data)

    async def _assemble_order_data_multi(
        self,
        customer: CustomerInfo,
        address: ShippingAddress,
        items: List[tuple],
        discount_code: str = None,
        customer_id: Optional[int] = None,
        financial_status: str = None,
        payment_gateway_names: list = None,
        note: str = None,
        transactions: list = None
    ) -> Dict[str, Any]:
        """Assemble Shopify order data for one order with multiple line items.

        ``items`` is a list of ``(product_dict, variant_dict, quantity)`` tuples.
        Discount handling mirrors the single-item ``_assemble_order_data``.
        """
        # Resolve discount once for the whole order.
        db_discount_code = None
        db_discount_fraction = None
        if discount_code:
            db_discount_code, db_discount_fraction = await aget_discount_details()

        line_items = []
        total_discount_amount = 0
        for product, variant, quantity in items:
            original_price = float(variant.get("price", 0))
            final_price = original_price

            if (
                discount_code
                and db_discount_code
                and db_discount_fraction
                and discount_code.upper() == db_discount_code.upper()
            ):
                item_discount = original_price * db_discount_fraction
                final_price = original_price - item_discount
                total_discount_amount += item_discount
                logger.debug(f"[DISCOUNT] {original_price:.0f} - {item_discount:.0f} ({db_discount_fraction*100}%) = {final_price:.0f} INR")

            line_items.append({
                "title": product.get("title", "Unknown"),
                "price": final_price,
                "quantity": quantity,
                "variant_id": variant["id"],
            })

        # Get address in Shopify format (includes name and phone with country code)
        shipping_address = address.to_shopify_format()

        # Format phone with country code for order level using same logic as address
        formatted_phone = self._format_phone_for_order(customer.phone)

        logger.debug(f"[ORDER] multi-item customer_id={customer_id} items={len(line_items)} city={shipping_address.get('city')}")

        effective_financial_status = financial_status or "pending"
        effective_payment_gateways = payment_gateway_names or ["Cash on Delivery (COD)"]

        order_data = {
            "line_items": line_items,
            "currency": "INR",
            "send_receipt": False,
            "send_fulfillment_receipt": False,
            "financial_status": effective_financial_status,
            "payment_gateway_names": effective_payment_gateways,
            "shipping_address": shipping_address,
            "billing_address": shipping_address,
            "tags": OrderTag.BLOOMERCE_CREATED
        }

        if note:
            order_data["note"] = note

        if customer_id:
            order_data["customer"] = {"id": customer_id}
        else:
            order_data["email"] = customer.email
            order_data["phone"] = formatted_phone

        if discount_code:
            order_data["discount_codes"] = [{"code": discount_code}]
            if total_discount_amount > 0:
                order_data["tags"] += f",DISCOUNT_APPLIED:{discount_code}:{total_discount_amount:.2f}"

        if transactions:
            order_data["transactions"] = transactions

        return order_data

    async def _acreate_shopify_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"https://{self.shopify_env.shop_url}/admin/api/{self.shopify_env.api_version}/orders.json"
        headers = {
            "X-Shopify-Access-Token": self.shopify_env.access_token,
            "Content-Type": "application/json"
        }

        try:
            client = await get_shared_async_http_client()
            _t0 = time.monotonic()
            response = await client.post(
                url,
                json={"order": order_data},
                headers=headers,
                timeout=15,
            )
            _elapsed = int((time.monotonic() - _t0) * 1000)
            if response.status_code == 201:
                response_data = response.json()
                order = response_data.get("order", {})
                logger.info(f"[SHOPIFY] POST create_order elapsed_ms={_elapsed} order={order.get('name')}")
                return {
                    "success": True,
                    "order": order,
                    "details": response_data
                }

            try:
                error_json = response.json()
            except Exception:
                error_json = {}
            return {
                "success": False,
                "error": f"Shopify API error: {response.status_code}",
                "details": error_json or response.text
            }
        except httpx.TimeoutException:
            return {"success": False, "error": "Request timed out while creating order"}
        except httpx.HTTPError as e:
            return {"success": False, "error": f"Network connection error while creating order: {str(e)}"}
        except Exception as e:
            return {"success": False, "error": f"Unexpected error: {str(e)}"}

async def acreate_order_from_data(
    customer_email: str,
    customer_phone: str,
    address_data: Dict[str, str],
    product_id: Union[str, int],
    variant_id: Union[str, int],
    quantity: int,
    shopify_env: Dict[str, str],
    discount_code: str = None
) -> Dict[str, Any]:
    """
    Convenience function to create order from raw data.
    
    Args:
        customer_email: Customer email address
        customer_phone: Customer phone number
        address_data: Address dictionary with keys: first_name, last_name, address1, city, state, zip, phone, [address2]
        product_id: Product ID (numeric)
        variant_id: Variant ID
        quantity: Order quantity
        shopify_env: Shopify environment dict with access_token, shop_url, api_version
        discount_code: Optional discount code to apply to the order
        
    Returns:
        Dict with success status, order data, and error details
    """
    # Create data classes
    customer = CustomerInfo(email=customer_email, phone=customer_phone)
    address = ShippingAddress(
        first_name=address_data.get("first_name", ""),
        last_name=address_data.get("last_name", ""),
        address1=address_data.get("address1", ""),
        address2=address_data.get("address2", ""),
        city=address_data.get("city", ""),
        state=address_data.get("state", ""),
        zip_code=address_data.get("zip", ""),
        phone=address_data.get("phone", "")
    )
    product = ProductSelection(
        product_id=product_id,
        variant_id=variant_id,
        quantity=quantity
    )
    env = ShopifyEnvironment(
        access_token=shopify_env["access_token"],
        shop_url=shopify_env["shop_url"],
        api_version=shopify_env.get("api_version", "2025-07")
    )
    
    # Create API instance and create order
    api = OrderCreationAPI(env)
    return await api.acreate_order(customer, address, product, discount_code)

async def aget_discount_details(client_id: Optional[str] = None) -> tuple[Optional[str], Optional[float]]:
    """
    Get discount code and fraction from PostgreSQL database (async).
    """
    try:
        from fashion_bot.database_manager import get_async_postgres_connection
        import json

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                if client_id:
                    await cur.execute("""
                        SELECT config_value
                        FROM client_configs
                        WHERE client_id = %s AND config_key = %s
                    """, (client_id, 'primary_discount_coupon'))
                else:
                    await cur.execute("""
                        SELECT config_value
                        FROM client_configs
                        WHERE config_key = %s
                        LIMIT 1
                    """, ('primary_discount_coupon',))

                result = await cur.fetchone()

        if result and result.get('config_value'):
            config_value = result['config_value']
            if isinstance(config_value, dict):
                data = config_value
            else:
                data = json.loads(config_value)

            if data and isinstance(data, dict):
                discount_code = list(data.keys())[0]
                discount_fraction = list(data.values())[0]
                logger.info(f"[DISCOUNT] Retrieved from database - Code: {discount_code}, Fraction: {discount_fraction}")
                return discount_code, float(discount_fraction)

        return None, None

    except Exception as e:
        logger.error(f"[DISCOUNT] Error fetching discount details: {e}")
        return None, None



async def amain() -> None:
    # Example usage - using config manager
    from fashion_bot.config_manager import aget_shopify_config

    shopify_config = await aget_shopify_config()
    if not shopify_config.get('access_token') or not shopify_config.get('shop_url'):
        print("Error: Shopify configuration not found in database")
        exit(1)
    
    env = ShopifyEnvironment(
        access_token=shopify_config.get('access_token'),
        shop_url=shopify_config.get('shop_url')
    )
    
    customer = CustomerInfo(
        email="",
        phone="9876543210"
    )
    
    address = ShippingAddress(
        first_name="TEST TEST CUSTOMER",
        last_name="TEST TEST   CUSTOMER",
        address1="123 Main Street",
        city="Mumbai",
        state="Maharashtra",
        zip_code="000000",
        phone="9876543210"
    )
    
    product = ProductSelection(
        product_id=10016261898562,  # Product ID
        variant_id=51012444717378,           # Variant ID
        quantity=1
    )
    
    api = OrderCreationAPI(env)
    result = await api.acreate_order(customer, address, product)
    print(result)


if __name__ == "__main__":
    asyncio.run(amain())
