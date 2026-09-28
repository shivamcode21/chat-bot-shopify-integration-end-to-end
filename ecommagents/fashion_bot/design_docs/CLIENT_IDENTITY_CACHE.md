# Client Identity Cache

Unified tiered cache for all external-identifier-to-`client_id` resolution.

## Problem

Six separate code paths resolved `client_id` from external identifiers
(shop domain, Gupshup phone, app name, website domain, client name) with
inconsistent caching: some had full tiered cache, some had Redis only,
and some hit Postgres on every request. Cache invalidation was
impossible without restarting services.

## Solution

A single module (`fashion_bot/utils/client_identity_cache.py`) wraps
every resolution pattern behind `aget_with_tiered_cache` (memory →
Redis → Postgres). All cache keys share the `cid:` prefix for easy
bulk invalidation.

### Cache key convention

```
cid:shop:{normalized_shop_domain}
cid:gup_src:{gupshup_source_phone}
cid:gup_app:{app_name}
cid:domain:{normalized_website_domain}
cid:name:{normalized_client_name}
```

### TTL

Controlled by `CLIENT_IDENTITY_CACHE_TTL` env (default **600 seconds** /
10 minutes) for both memory and Redis tiers.

### Resolution functions

| Function | Input | SQL target |
|----------|-------|------------|
| `aget_client_id_by_shop_domain` | Shopify shop domain | `clients.shopify_domain_name` |
| `aget_client_id_by_gupshup_source` | Gupshup business phone | `clients.gupshup_source_number` |
| `aget_client_id_by_gupshup_app_name` | Gupshup APP_NAME | `client_configs.config_value->>'APP_NAME'` |
| `aget_client_id_by_website_domain` | Website domain | `clients.domain` (normalized) |
| `aget_client_id_by_client_name` | Client display name | `clients.name` (case-insensitive) |

All functions return `Optional[str]` (`None` on miss). Negative results
are cached in memory only (not written to Redis) to protect Postgres
from repeated lookups for unmapped identifiers.

### Invalidation

#### Programmatic

```python
from fashion_bot.utils.client_identity_cache import (
    ainvalidate_shop_domain,
    ainvalidate_all,
)

await ainvalidate_shop_domain("example.myshopify.com")
await ainvalidate_all()  # flushes every cid:* key
```

Each `ainvalidate_*` helper clears both memory (process-local) and Redis.

#### HTTP admin endpoint

```
POST /admin/cache/invalidate
Authorization: Bearer <ADMIN_API_TOKEN>

{
  "identifier_type": "shop_domain",   // or gupshup_source, app_name, domain, client_name, all
  "value": "example.myshopify.com"    // required unless identifier_type is "all"
}
```

Protected by `ADMIN_API_TOKEN` env variable (must be set for the
endpoint to accept requests).

#### Multi-pod behavior

The admin endpoint clears Redis (visible to all pods on next cache miss)
and memory on the pod that receives the request. Other pods continue
serving from their local memory cache until TTL expires (max 10 min).
This is acceptable because client identity data changes extremely
rarely.

### Migrated call sites

| File | Old function | Now delegates to |
|------|-------------|-----------------|
| `shopify_webhook.py` | `aget_client_id_from_shop_domain` (tiered) | `aget_client_id_by_shop_domain` |
| `product_webhook.py` | `aget_client_id_from_shop_domain` (DB) | `aget_client_id_by_shop_domain` |
| `abandoned_checkout_webhook.py` | `aget_client_id_from_shop_domain` (DB) | `aget_client_id_by_shop_domain` |
| `config_manager.py` | `aresolve_client_id` (DB) | `aget_client_id_by_gupshup_source` |
| `config_manager.py` | `aget_client_id_by_app_name` (Redis) | `aget_client_id_by_gupshup_app_name` |
| `demo_chat_router.py` | `aget_pg_client_id_for_domain` (DB) | `aget_client_id_by_website_domain` |
| `attribution_router.py` | `aresolve_client_id` (dict) | `aget_client_id_by_client_name` |

Old function signatures are preserved as thin wrappers for backward
compatibility.
