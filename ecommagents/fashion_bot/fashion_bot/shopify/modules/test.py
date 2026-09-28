"""
Modular API for Shopify Order Creation
Provides complete workflows for creating orders with proper validation and error handling.
"""

import requests
from typing import Dict, Any, Optional, List
import re
import json
import os
import random
import string
from dataclasses import dataclass

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
        """Validate Indian phone number (10 digits, optional +91/91)."""
        return re.match(r"^(\+91|91)?\d{10}$", phone) is not None

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
        elif len(self.address1) < 10 or not any(c.isdigit() for c in self.address1) or not any(c.isalpha() for c in self.address1):
            errors.append("Address must be at least 10 characters and contain both letters and numbers")
        
        if not self.zip_code:
            errors.append("PIN/ZIP Code is required")
        
        if not self.phone:
            errors.append("Phone number is required")
        elif not CustomerInfo._is_valid_phone(self.phone):
            errors.append("Invalid phone number format")
        
        # Validate PIN code and city/state consistency
        pin_info = self._lookup_pincode_online(self.zip_code)
        if pin_info and pin_info.get("city") and pin_info.get("state"):
            city_match = self.city.strip().lower() == pin_info["city"].strip().lower()
            state_match = self.state.strip().lower() == pin_info["state"].strip().lower()
            if not (city_match and state_match):
                errors.append(f"PIN {self.zip_code} does not match city/state (expected {pin_info['city']}, {pin_info['state']})")
        
        return {"valid": len(errors) == 0, "errors": errors}
    
    @staticmethod
    def _lookup_pincode_online(pin_code: str) -> Optional[Dict[str, str]]:
        """Lookup PIN code information from online API."""
        try:
            url = f"https://api.postalpincode.in/pincode/{pin_code}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            }
            resp = requests.get(url, headers=headers, timeout=10)
            data = resp.json()

            if (isinstance(data, list) and data and
                data[0].get("Status") == "Success" and
                "PostOffice" in data[0] and data[0]["PostOffice"]):

                po = data[0]["PostOffice"][0]
                return {
                    "city": po.get("District", ""),
                    "state": po.get("State", "")
                }
        except Exception as e:
            print(f"[DEBUG] Exception during PIN lookup: {e}")
        return None
    
    def to_shopify_format(self) -> Dict[str, str]:
        """Convert to Shopify shipping address format."""
        return {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "address1": self.address1,
            "address2": self.address2,
            "city": self.city.title(),
            "province": self.state,
            "zip": self.zip_code,
            "country": self.country,
            "phone": self.phone
        }

@dataclass
class ProductSelection:
    """Product selection information."""
    query: str
    quantity: int
    variant_id: Optional[str] = None
    
    def validate(self) -> Dict[str, Any]:
        """Validate product selection."""
        errors = []
        
        if not self.query:
            errors.append("Product query is required")
        
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
    
    def create_order(
        self,
        customer: CustomerInfo,
        address: ShippingAddress,
        product: ProductSelection
    ) -> Dict[str, Any]:
        """
        Create a Shopify order with complete validation and error handling.
        
        Args:
            customer: Customer information
            address: Shipping address
            product: Product selection
            
        Returns:
            Dict with success status, order data, and error details
        """
        # Step 1: Validate all inputs
        validation_result = self._validate_inputs(customer, address, product)
        if not validation_result["valid"]:
            return {
                "success": False,
                "error": "Validation failed",
                "details": validation_result["errors"]
            }
        
        # Step 2: Find product and variant
        product_result = self._find_product_and_variant(product)
        if not product_result["success"]:
            return product_result
        
        # Step 3: Assemble order data
        order_data = self._assemble_order_data(customer, address, product_result["product"], product_result["variant"], product.quantity)
        
        # Step 4: Create order in Shopify
        return self._create_shopify_order(order_data)
    
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
    
    def _find_product_and_variant(self, product: ProductSelection) -> Dict[str, Any]:
        """Find product and variant based on query."""
        try:
            from shopify.modules.product_handlers import update_product_cache, load_product_cache, fuzzy_search_products
        except ImportError:
            return {
                "success": False,
                "error": "Product handlers module not found"
            }
        
        # Update product cache
        cache_result = update_product_cache(
            self.shopify_env.access_token,
            self.shopify_env.shop_url,
            self.shopify_env.api_version
        )
        if not (isinstance(cache_result, dict) and cache_result.get("success", True)):
            return {
                "success": False,
                "error": f"Error updating product cache: {cache_result.get('error', 'Unknown error')}"
            }
        
        # Load products
        products = load_product_cache()
        if isinstance(products, dict) and not products.get("success", True):
            return {
                "success": False,
                "error": f"Error loading product cache: {products.get('error', 'Unknown error')}"
            }
        if not isinstance(products, list):
            return {
                "success": False,
                "error": "Product cache is not a valid list"
            }
        
        # Search for products
        matches = fuzzy_search_products(product.query, products)
        if not matches:
            return {
                "success": False,
                "error": "No matching products found for query",
                "details": {"query": product.query}
            }
        
        if len(matches) > 1:
            return {
                "success": False,
                "error": "Multiple products found. Please refine your query",
                "details": {"matches": [p.get('title') for p in matches]}
            }
        
        selected_product = matches[0]
        variants = selected_product.get('variants', [])
        if not variants:
            return {
                "success": False,
                "error": "No variants available for selected product",
                "details": {"product": selected_product.get('title')}
            }
        
        # Select variant
        selected_variant = variants[0]  # Default to first variant
        if product.variant_id:
            # Find specific variant if provided
            for variant in variants:
                if str(variant.get("id")) == product.variant_id:
                    selected_variant = variant
                    break
        
        return {
            "success": True,
            "product": selected_product,
            "variant": selected_variant
        }
    
    def _assemble_order_data(
        self,
        customer: CustomerInfo,
        address: ShippingAddress,
        product: Dict[str, Any],
        variant: Dict[str, Any],
        quantity: int
    ) -> Dict[str, Any]:
        """Assemble Shopify order data."""
        line_item = {
            "title": product.get("title", "Unknown"),
            "price": float(variant.get("price", 0)) if variant else float(product.get("price", 0)),
            "quantity": quantity
        }
        
        if variant and variant.get("id"):
            line_item["variant_id"] = variant["id"]
        
        return {
            "line_items": [line_item],
            "currency": "INR",
            "email": customer.email,
            "send_receipt": False,
            "send_fulfillment_receipt": False,
            "financial_status": "pending",
            "payment_gateway_names": ["Cash on Delivery (COD)"],
            "shipping_address": address.to_shopify_format()
        }
    
    def _create_shopify_order(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create order in Shopify."""
        url = f"https://{self.shopify_env.shop_url}/admin/api/{self.shopify_env.api_version}/orders.json"
        headers = {
            "X-Shopify-Access-Token": self.shopify_env.access_token,
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.post(url, json={"order": order_data}, headers=headers, timeout=15)
            if response.status_code == 201:
                return {
                    "success": True,
                    "order": response.json().get("order", {}),
                    "details": response.json()
                }
            else:
                try:
                    error_json = response.json()
                except Exception:
                    error_json = {}
                return {
                    "success": False,
                    "error": f"Shopify API error: {response.status_code}",
                    "details": error_json or response.text
                }
        except requests.Timeout:
            return {"success": False, "error": "Request timed out while creating order"}
        except requests.ConnectionError:
            return {"success": False, "error": "Network connection error while creating order"}
        except Exception as e:
            return {"success": False, "error": f"Unexpected error: {str(e)}"}

def create_order_from_data(
    customer_email: str,
    customer_phone: str,
    address_data: Dict[str, str],
    product_query: str,
    quantity: int,
    shopify_env: Dict[str, str]
) -> Dict[str, Any]:
    """
    Convenience function to create order from raw data.
    
    Args:
        customer_email: Customer email address
        customer_phone: Customer phone number
        address_data: Address dictionary with keys: first_name, last_name, address1, city, state, zip, phone, [address2]
        product_query: Product search query
        quantity: Order quantity
        shopify_env: Shopify environment dict with access_token, shop_url, api_version
        
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
    product = ProductSelection(query=product_query, quantity=quantity)
    env = ShopifyEnvironment(
        access_token=shopify_env["access_token"],
        shop_url=shopify_env["shop_url"],
        api_version=shopify_env.get("api_version", "2025-07")
    )
    
    # Create API instance and create order
    api = OrderCreationAPI(env)
    return api.create_order(customer, address, product)

if __name__ == "__main__":
    # Example usage
    env = ShopifyEnvironment(
        access_token="your_access_token",
        shop_url="your-store.myshopify.com"
    )
    
    customer = CustomerInfo(
        email="customer@example.com",
        phone="9876543210"
    )
    
    address = ShippingAddress(
        first_name="John",
        last_name="Doe",
        address1="123 Main Street",
        city="Mumbai",
        state="Maharashtra",
        zip_code="400001",
        phone="9876543210"
    )
    
    product = ProductSelection(
        query="jeans",
        quantity=1
    )
    
    api = OrderCreationAPI(env)
    result = api.create_order(customer, address, product)
    print(result) 