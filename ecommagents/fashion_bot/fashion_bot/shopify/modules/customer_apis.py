"""
Customer APIs for Shopify
Provides functions to search and fetch customer data from Shopify.
"""

from typing import Dict, Any, Optional
import logging
import re
import time
import httpx

from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)


def _normalize_phone_variants(phone: str) -> Optional[list]:
    # Strip spaces, dashes, dots and any other separators before validating so
    # a number like "+91 8075053289" is accepted instead of rejected.
    digits = re.sub(r"\D", "", phone or "")
    # Strip a +91/91 country code or a single leading 0 trunk prefix.
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'):
        digits = digits[1:]
    normalized_phone = digits

    if not normalized_phone.isdigit() or len(normalized_phone) != 10:
        logger.warning(f"Invalid phone number format: {phone}")
        return None

    return [
        normalized_phone,
        f"+91{normalized_phone}",
        f"91{normalized_phone}",
    ]


def _format_customer_node(customer_node: Dict[str, Any]) -> Dict[str, Any]:
    customer_data = {
        "id": customer_node.get("id", "").split("/")[-1],
        "email": customer_node.get("email"),
        "first_name": customer_node.get("firstName"),
        "last_name": customer_node.get("lastName"),
        "phone": customer_node.get("phone"),
        "default_address": None,
        "addresses": []
    }

    if customer_node.get("defaultAddress"):
        default_addr = customer_node["defaultAddress"]
        customer_data["default_address"] = {
            "first_name": default_addr.get("firstName"),
            "last_name": default_addr.get("lastName"),
            "address1": default_addr.get("address1"),
            "address2": default_addr.get("address2"),
            "city": default_addr.get("city"),
            "province": default_addr.get("province"),
            "zip": default_addr.get("zip"),
            "phone": default_addr.get("phone"),
            "country": default_addr.get("country", "India")
        }

    for addr in customer_node.get("addresses", []):
        customer_data["addresses"].append({
            "first_name": addr.get("firstName"),
            "last_name": addr.get("lastName"),
            "address1": addr.get("address1"),
            "address2": addr.get("address2"),
            "city": addr.get("city"),
            "province": addr.get("province"),
            "zip": addr.get("zip"),
            "phone": addr.get("phone"),
            "country": addr.get("country", "India")
        })

    return customer_data
async def asearch_customer_by_phone(
    phone: str,
    shopify_config: Dict[str, str]
) -> Optional[Dict[str, Any]]:
    try:
        phone_variants = _normalize_phone_variants(phone)
        if not phone_variants:
            return None

        access_token = shopify_config.get('access_token')
        shop_url = shopify_config.get('shop_url')
        api_version = shopify_config.get('api_version', '2024-04')

        if not access_token or not shop_url:
            logger.error("Missing Shopify credentials")
            return None

        url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
        query = """
            query searchCustomers($query: String!) {
                customers(first: 5, query: $query) {
                    edges {
                        node {
                            id
                            email
                            firstName
                            lastName
                            phone
                            defaultAddress {
                                id
                                firstName
                                lastName
                                address1
                                address2
                                city
                                province
                                zip
                                phone
                                country
                            }
                            addresses(first: 10) {
                                id
                                firstName
                                lastName
                                address1
                                address2
                                city
                                province
                                zip
                                phone
                                country
                            }
                        }
                    }
                }
            }
        """
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token
        }

        client = await get_shared_async_http_client()
        for phone_variant in phone_variants:
            payload = {
                "query": query,
                "variables": {"query": f"phone:{phone_variant}"},
            }
            _t0 = time.monotonic()
            response = await client.post(url, json=payload, headers=headers, timeout=10)
            _elapsed = int((time.monotonic() - _t0) * 1000)

            if response.status_code != 200:
                logger.warning(f"[SHOPIFY] GraphQL customer search elapsed_ms={_elapsed} status={response.status_code}")
                continue

            data = response.json()
            if "errors" in data:
                logger.warning(f"[SHOPIFY] GraphQL customer search errors elapsed_ms={_elapsed}")
                continue

            customers = data.get("data", {}).get("customers", {}).get("edges", [])
            if not customers:
                continue

            customer_data = _format_customer_node(customers[0]["node"])
            logger.info(f"[SHOPIFY] Customer found elapsed_ms={_elapsed} id={customer_data.get('id')}")
            return customer_data

        logger.info(f"[SHOPIFY] No customer found for phone ***{str(phone)[-4:]}")
        return None
    except httpx.HTTPError as e:
        logger.error(f"HTTP error searching customer by phone: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error searching customer by phone: {str(e)}")
        return None
async def afetch_customer_data_from_shopify(phone: str, shopify_config: Dict[str, str]) -> Dict[str, Any]:
    result = {
        "found": False,
        "customer_name": None,
        "customer_address": None,
        "email": None,
        "phone": None
    }

    try:
        customer_data = await asearch_customer_by_phone(phone, shopify_config)

        if not customer_data:
            logger.info("📝 New customer - will need to collect full information")
            return result

        result["found"] = True
        result["email"] = customer_data.get("email")
        result["phone"] = customer_data.get("phone")

        first_name = (customer_data.get("first_name") or "").strip()
        last_name = (customer_data.get("last_name") or "").strip()
        if first_name and last_name:
            result["customer_name"] = f"{first_name} {last_name}"
        elif first_name:
            result["customer_name"] = first_name
        elif last_name:
            result["customer_name"] = last_name

        address = customer_data.get("default_address")
        if not address and customer_data.get("addresses"):
            address = customer_data["addresses"][0]

        if address:
            address_parts = []
            if address.get("address1"):
                address_parts.append(address["address1"])
            if address.get("address2"):
                address_parts.append(address["address2"])
            if address.get("city"):
                address_parts.append(address["city"])
            if address.get("province"):
                address_parts.append(address["province"])
            if address.get("zip"):
                address_parts.append(address["zip"])
            if address_parts:
                result["customer_address"] = ", ".join(address_parts)

        return result
    except Exception as e:
        logger.error(f"Error in afetch_customer_data_from_shopify: {str(e)}")
        return result
