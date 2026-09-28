"""
Cron job to refresh Shopify access token daily.

Shopify client_credentials tokens expire every 24 hours (86399 seconds).
This job refreshes the token and updates client_configs in PostgreSQL.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any

from fashion_bot.database_manager import awith_retry, get_async_postgres_connection
from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)

# The client_id in our database
DB_CLIENT_ID = "81e80e20-fe91-470a-ab3d-e9dfc2eebf4a"


@awith_retry
async def _aget_shopify_credentials(client_id: str) -> Dict[str, str]:
    query = """
        SELECT config_value
        FROM client_configs
        WHERE client_id = %s
          AND config_key = 'shopify_details'
    """

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, (client_id,))
            row = await cur.fetchone()

    if not row:
        raise ValueError(f"No shopify_details config found for client_id={client_id}")

    config = row["config_value"]
    shopify_domain = config.get("SHOPIFY_DOMAIN")
    shopify_client_id = config.get("SHOPIFY_CLIENT_ID")
    shopify_client_secret = config.get("SHOPIFY_CLIENT_SECRET")

    if not all([shopify_domain, shopify_client_id, shopify_client_secret]):
        missing = [
            key
            for key, value in {
                "SHOPIFY_DOMAIN": shopify_domain,
                "SHOPIFY_CLIENT_ID": shopify_client_id,
                "SHOPIFY_CLIENT_SECRET": shopify_client_secret,
            }.items()
            if not value
        ]
        raise ValueError(f"Missing keys in shopify_details config: {missing}")

    return {
        "oauth_url": f"https://{shopify_domain}/admin/oauth/access_token",
        "client_id": shopify_client_id,
        "client_secret": shopify_client_secret,
    }


async def arefresh_shopify_token() -> Dict[str, Any]:
    try:
        print("\n" + "=" * 80, flush=True)
        print(
            f"[CRON_SHOPIFY_TOKEN] Starting Shopify token refresh at {datetime.now(timezone.utc).isoformat()}",
            flush=True,
        )
        print("=" * 80, flush=True)

        print("[CRON_SHOPIFY_TOKEN] 📖 Reading Shopify credentials from database...", flush=True)
        credentials = await _aget_shopify_credentials(DB_CLIENT_ID)
        print(
            f"[CRON_SHOPIFY_TOKEN] ✅ Got credentials — OAuth URL: {credentials['oauth_url']}",
            flush=True,
        )

        print("[CRON_SHOPIFY_TOKEN] 🔄 Requesting new token from Shopify...", flush=True)
        client = await get_shared_async_http_client()
        response = await client.post(
            credentials["oauth_url"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
            },
            timeout=30,
        )

        if response.status_code != 200:
            error_msg = f"Shopify OAuth returned status {response.status_code}: {response.text}"
            logger.error(f"[CRON_SHOPIFY_TOKEN] ❌ {error_msg}")
            print(f"[CRON_SHOPIFY_TOKEN] ❌ {error_msg}", flush=True)
            return {
                "success": False,
                "error": error_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        token_data = response.json()
        new_access_token = token_data.get("access_token")
        expires_in = token_data.get("expires_in")

        if not new_access_token:
            error_msg = f"No access_token in Shopify response: {token_data}"
            logger.error(f"[CRON_SHOPIFY_TOKEN] ❌ {error_msg}")
            print(f"[CRON_SHOPIFY_TOKEN] ❌ {error_msg}", flush=True)
            return {
                "success": False,
                "error": error_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        print(f"[CRON_SHOPIFY_TOKEN] ✅ Got new token (expires_in={expires_in}s)", flush=True)
        print(f"[CRON_SHOPIFY_TOKEN] Token prefix: {new_access_token[:10]}...", flush=True)
        print("[CRON_SHOPIFY_TOKEN] 💾 Updating client_configs in database...", flush=True)

        update_query = """
            UPDATE client_configs
            SET config_value = jsonb_set(
                config_value,
                '{SHOPIFY_TOKEN}',
                %s::jsonb,
                false
            )
            WHERE client_id = %s
              AND config_key = 'shopify_details'
        """

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(update_query, (f'"{new_access_token}"', DB_CLIENT_ID))
                rows_updated = cur.rowcount

        if rows_updated == 0:
            error_msg = f"No rows updated — check client_id={DB_CLIENT_ID} has shopify_details config"
            logger.warning(f"[CRON_SHOPIFY_TOKEN] ⚠️ {error_msg}")
            print(f"[CRON_SHOPIFY_TOKEN] ⚠️ {error_msg}", flush=True)
            return {
                "success": False,
                "error": error_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        print(f"[CRON_SHOPIFY_TOKEN] ✅ Database updated ({rows_updated} row(s))", flush=True)
        print("[CRON_SHOPIFY_TOKEN] ✅ Token refresh complete!", flush=True)
        print("=" * 80 + "\n", flush=True)

        logger.info(f"[CRON_SHOPIFY_TOKEN] Token refreshed successfully for client {DB_CLIENT_ID}")
        return {
            "success": True,
            "rows_updated": rows_updated,
            "expires_in": expires_in,
            "token_prefix": new_access_token[:10] + "...",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        error_msg = f"Error in Shopify token refresh cron job: {e}"
        logger.error(f"[CRON_SHOPIFY_TOKEN] ❌ {error_msg}")
        print(f"\n[CRON_SHOPIFY_TOKEN] ❌ {error_msg}\n", flush=True)
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def refresh_shopify_token() -> Dict[str, Any]:
    return asyncio.run(arefresh_shopify_token())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = refresh_shopify_token()
    print(f"\nResult: {result}")
