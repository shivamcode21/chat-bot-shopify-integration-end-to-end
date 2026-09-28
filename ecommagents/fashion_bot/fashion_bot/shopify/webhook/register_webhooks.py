"""
Register Shopify webhooks for payment and fulfillment status events.
"""
import asyncio
import logging
from typing import Dict, List, Optional

# Import your config manager (adjust import if needed)
from fashion_bot.config_manager import aget_shopify_config as _aget_shopify_config
from fashion_bot.utils.http_client import get_shared_async_http_client

# Topics to subscribe to
WEBHOOK_TOPICS = [
    "orders/paid",
    "orders/partially_paid",
    "orders/fulfilled",
    "orders/updated"
]

# Product webhook topics (for vector DB sync)
PRODUCT_WEBHOOK_TOPICS = [
    "products/create",
    "products/update", 
    "products/delete"
]

# Inventory webhook topics (real-time stock sync)
INVENTORY_WEBHOOK_TOPICS = [
    "inventory_levels/update",
]

# The endpoint on your server that will receive the webhooks
WEBHOOK_ENDPOINT = "/shopify/webhook"
PRODUCT_WEBHOOK_ENDPOINT = "/product/webhook/products"
INVENTORY_WEBHOOK_ENDPOINT = "/product/webhook/inventory"

# Helper to get full webhook URL (adjust host as needed)
def get_webhook_url(host: str) -> str:
    return f"{host.rstrip('/')}{WEBHOOK_ENDPOINT}"

# Metafield namespaces to include in product webhooks.
# These match the namespaces used by ShopifyProductService._normalize_product()
# for extracting fabric, care_instructions, fit_type, and size_chart.
PRODUCT_METAFIELD_NAMESPACES = ["custom", "product"]


# Register a single webhook
async def register_webhook(
    shop_url: str,
    access_token: str,
    api_version: str,
    topic: str,
    address: str,
    metafield_namespaces: list = None,
) -> Dict:
    url = f"https://{shop_url}/admin/api/{api_version}/webhooks.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json"
    }
    webhook_payload = {
        "topic": topic,
        "address": address,
        "format": "json"
    }
    # Include metafield namespaces if provided (Shopify will include
    # metafields from these namespaces in the webhook payload)
    if metafield_namespaces:
        webhook_payload["metafield_namespaces"] = metafield_namespaces
    payload = {"webhook": webhook_payload}
    client = await get_shared_async_http_client()
    resp = await client.post(url, json=payload, headers=headers, timeout=10)
    try:
        return resp.json()
    except Exception:
        return {"error": resp.text}

async def amain():
    logging.basicConfig(level=logging.INFO)
    config = await _aget_shopify_config()
    shop_url = config.get("shop_url")
    access_token = config.get("access_token")
    api_version = config.get("api_version", "2024-04")
    # You may want to set this to your public server URL
    host = "https://yourdomain.com"  # <-- CHANGE THIS
    webhook_url = get_webhook_url(host)
    product_webhook_url = f"{host.rstrip('/')}{PRODUCT_WEBHOOK_ENDPOINT}"
    
    if not shop_url or not access_token:
        logging.error("Missing Shopify credentials. Check your config.")
        return
    
    # Register order webhooks
    for topic in WEBHOOK_TOPICS:
        logging.info(f"Registering webhook for topic: {topic} -> {webhook_url}")
        result = await register_webhook(shop_url, access_token, api_version, topic, webhook_url)
        logging.info(f"Result: {result}")
    
    # Register product webhooks (for vector DB sync) with metafield namespaces
    # so Shopify includes metafields (fabric, care, fit, size_chart) in the payload
    for topic in PRODUCT_WEBHOOK_TOPICS:
        logging.info(f"Registering product webhook for topic: {topic} -> {product_webhook_url} (metafield_namespaces={PRODUCT_METAFIELD_NAMESPACES})")
        result = await register_webhook(
            shop_url, access_token, api_version, topic, product_webhook_url,
            metafield_namespaces=PRODUCT_METAFIELD_NAMESPACES
        )
        logging.info(f"Result: {result}")
    
    # Register inventory webhooks (real-time stock level sync)
    inventory_webhook_url = f"{host.rstrip('/')}{INVENTORY_WEBHOOK_ENDPOINT}"
    for topic in INVENTORY_WEBHOOK_TOPICS:
        logging.info(f"Registering inventory webhook for topic: {topic} -> {inventory_webhook_url}")
        result = await register_webhook(
            shop_url, access_token, api_version, topic, inventory_webhook_url,
        )
        logging.info(f"Result: {result}")

def main():
    asyncio.run(amain())

if __name__ == "__main__":
    main()
