from typing import Any, Dict, Optional

import httpx

from fashion_bot.utils.http_client import get_shared_async_http_client


async def aget_order_from_shopify(
    order_id: str,
    shop_url: str,
    access_token: str,
    api_version: str = "2023-07",
) -> Optional[Dict[str, Any]]:
    url = f"https://{shop_url}/admin/api/{api_version}/orders/{order_id}.json"
    headers = {"X-Shopify-Access-Token": access_token}

    try:
        client = await get_shared_async_http_client()
        response = await client.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError:
        return None
