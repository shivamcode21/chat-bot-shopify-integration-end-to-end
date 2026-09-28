"""Admin API router for cache management and operational controls.

Endpoints are protected by a bearer token set via the ``ADMIN_API_TOKEN``
environment variable.  If the env var is unset, all admin endpoints
return 403.
"""

import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from fashion_bot.env_loader import get_env

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])

_ADMIN_TOKEN: Optional[str] = get_env("ADMIN_API_TOKEN")


def _check_auth(request: Request) -> None:
    if not _ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Admin API is not configured")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != _ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")


class CacheInvalidateRequest(BaseModel):
    identifier_type: str = "all"
    value: Optional[str] = None


@router.post("/cache/invalidate")
async def invalidate_cache(request: Request, body: CacheInvalidateRequest = CacheInvalidateRequest()):
    """Invalidate client identity caches.

    ``identifier_type`` can be one of:
    ``shop_domain``, ``gupshup_source``, ``app_name``, ``domain``,
    ``client_name``, or ``all`` (default).

    When ``identifier_type`` is not ``all``, ``value`` must contain the
    specific identifier string to invalidate.
    """
    _check_auth(request)

    from fashion_bot.utils.client_identity_cache import (
        INVALIDATION_DISPATCH,
        ainvalidate_all,
    )

    id_type = body.identifier_type
    value = body.value

    if id_type == "all":
        count = await ainvalidate_all()
        logger.info(f"[ADMIN] Invalidated all client identity caches ({count} keys)")
        return {"status": "ok", "invalidated": count, "scope": "all"}

    invalidator = INVALIDATION_DISPATCH.get(id_type)
    if not invalidator:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown identifier_type '{id_type}'. "
                   f"Valid: {', '.join(sorted(INVALIDATION_DISPATCH))} or 'all'.",
        )

    if not value:
        raise HTTPException(
            status_code=400,
            detail=f"'value' is required when identifier_type is '{id_type}'",
        )

    await invalidator(value)
    logger.info(f"[ADMIN] Invalidated cache for {id_type}={value}")
    return {"status": "ok", "invalidated": 1, "scope": id_type, "value": value}


@router.get("/cache/stats")
async def cache_stats(request: Request):
    """Return tiered cache hit/miss counters for this pod."""
    _check_auth(request)
    from fashion_bot.utils.tiered_cache import get_cache_stats
    return get_cache_stats()


# ===========================================================================
# Shopify shop-domain mapping
# ===========================================================================
# The Shopify shop domain is stored in TWO tables and nothing keeps them in
# step:
#
#   * ``shopify_stores.shop_domain``   — written by the OAuth install callback
#     (every production row carries ``created_by='oauth'``); drives ingestion.
#   * ``clients.shopify_domain_name``  — read by
#     ``client_identity_cache.aget_client_id_by_shop_domain`` to resolve the
#     tenant for EVERY inbound Shopify webhook.
#
# When only one is set, the tenant looks fully live on every dashboard while
# its webhooks are silently discarded — the handler acknowledges with HTTP 200
# by design so Shopify does not retry for hours, so the only symptom is a log
# line. ``vahro.myshopify.com`` lost 2,345 webhooks over four days that way.
#
# These endpoints make the pairing explicit: one write updates BOTH tables in a
# single transaction and reports exactly what changed, and the read endpoint
# shows both values side by side so a mismatch is visible before it bites.

_SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")


def _normalize_shop_domain(raw: Optional[str]) -> str:
    """Lower-case, trim, and strip any scheme/path/port a human pasted in.

    ``https://Vahro.myshopify.com/admin`` and ``vahro.myshopify.com`` must end
    up byte-identical, because resolution compares this against the raw
    ``X-Shopify-Shop-Domain`` header with ``=``.
    """
    text = (raw or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^https?://", "", text)
    text = text.split("/", 1)[0]
    text = text.split(":", 1)[0]
    return text.strip()


def _classify_mapping(clients_domain: Optional[str], store_domains: List[str]) -> Tuple[str, str]:
    """Return ``(status, human_readable_detail)`` for one client's mapping."""
    stores = [d for d in (store_domains or []) if d]

    if not clients_domain and not stores:
        return "unconfigured", "No shop domain set and no Shopify app installed."

    if not clients_domain:
        return (
            "clients_column_empty",
            f"The Shopify app is installed ({', '.join(stores)}) but the client "
            f"record has no shop domain, so inbound webhooks cannot be matched "
            f"to this client and are being discarded.",
        )

    if not _SHOP_DOMAIN_RE.match(clients_domain):
        return (
            "malformed",
            f"'{clients_domain}' is not a valid <store>.myshopify.com address, "
            f"so it can never match what Shopify sends.",
        )

    if not stores:
        return (
            "no_oauth_install",
            f"Shop domain is '{clients_domain}' but the Shopify app is not "
            f"installed, so there is no access token to read the catalog with.",
        )

    if clients_domain not in stores:
        return (
            "mismatch",
            f"The client record says '{clients_domain}' but the installed "
            f"store(s) are {', '.join(stores)}. They disagree — webhooks from "
            f"the installed stores will not be matched to this client.",
        )

    return "synced", f"Both places agree on '{clients_domain}'."


_LIST_SQL = """
    SELECT
        c.id::text                                           AS client_id,
        c.name                                               AS client_name,
        nullif(btrim(coalesce(c.shopify_domain_name,'')),'') AS clients_domain,
        s.store_domains
      FROM clients c
      LEFT JOIN (
            SELECT client_id,
                   array_agg(shop_domain ORDER BY shop_domain) AS store_domains
              FROM shopify_stores
             WHERE is_active IS TRUE
             GROUP BY client_id
           ) s ON s.client_id = c.id
     ORDER BY c.name;
"""


async def _fetch_mapping_rows() -> List[Dict[str, Any]]:
    from fashion_bot.database_manager import get_async_postgres_connection

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_LIST_SQL)
            rows = await cur.fetchall() or []

    out = []
    for row in rows:
        stores = list(row.get("store_domains") or [])
        status, detail = _classify_mapping(row.get("clients_domain"), stores)
        out.append({
            "client_id": row["client_id"],
            "client_name": row["client_name"],
            "clients_shopify_domain_name": row.get("clients_domain"),
            "shopify_stores_domains": stores,
            "status": status,
            "detail": detail,
        })
    return out


@router.get("/shopify/shop-domains")
async def list_shop_domains(request: Request, status: Optional[str] = None):
    """Both stored shop domains for every client, side by side, with status.

    Optional ``?status=`` filter. Statuses: ``synced``, ``clients_column_empty``,
    ``mismatch``, ``malformed``, ``no_oauth_install``, ``unconfigured``.
    """
    _check_auth(request)

    clients = await _fetch_mapping_rows()
    if status:
        clients = [c for c in clients if c["status"] == status]

    summary: Dict[str, int] = {}
    for entry in clients:
        summary[entry["status"]] = summary.get(entry["status"], 0) + 1

    needs_attention = [
        c for c in clients if c["status"] not in ("synced", "unconfigured")
    ]
    return {
        "status": "ok" if not needs_attention else "attention_required",
        "summary": summary,
        "total": len(clients),
        "needs_attention": len(needs_attention),
        "clients": clients,
    }


class ShopDomainUpdate(BaseModel):
    shop_domain: str


@router.put("/shopify/shop-domains/{client_id}")
async def set_shop_domain(client_id: str, request: Request, body: ShopDomainUpdate):
    """Set a client's Shopify shop domain in BOTH tables, atomically.

    This is the whole point of the endpoint: updating one table without the
    other is what silently breaks webhook delivery, so the two writes happen in
    one transaction and the response states plainly which tables changed.

    Rejects anything that is not a ``<store>.myshopify.com`` address, and
    refuses a domain already claimed by a different client.
    """
    _check_auth(request)

    from fashion_bot.database_manager import get_async_postgres_connection
    from fashion_bot.utils.client_identity_cache import ainvalidate_shop_domain

    try:
        uuid.UUID(client_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"'{client_id}' is not a valid client id.")

    domain = _normalize_shop_domain(body.shop_domain)
    if not domain:
        raise HTTPException(status_code=400, detail="Shop domain is required.")
    if not _SHOP_DOMAIN_RE.match(domain):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{domain}' is not a valid Shopify shop domain. It must look "
                f"like your-store.myshopify.com (this is the address Shopify "
                f"sends on every webhook, so anything else will never match)."
            ),
        )

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id::text AS id, name, "
                "nullif(btrim(coalesce(shopify_domain_name,'')),'') AS domain "
                "FROM clients WHERE id = %s;",
                (client_id,),
            )
            client = await cur.fetchone()
            if not client:
                raise HTTPException(status_code=404, detail=f"No client with id {client_id}.")

            # shopify_stores.shop_domain is globally UNIQUE, so a domain owned by
            # another tenant must be rejected before we try to write it.
            await cur.execute(
                "SELECT c.name AS name, s.client_id::text AS client_id "
                "  FROM shopify_stores s JOIN clients c ON c.id = s.client_id "
                " WHERE s.shop_domain = %s AND s.client_id::text <> %s "
                " LIMIT 1;",
                (domain, client_id),
            )
            clash = await cur.fetchone()
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"'{domain}' is already the installed store for "
                        f"{clash['name']}. A shop can only belong to one client."
                    ),
                )

            await cur.execute(
                "SELECT id::text AS id, shop_domain FROM shopify_stores "
                " WHERE client_id = %s AND is_active IS TRUE ORDER BY shop_domain;",
                (client_id,),
            )
            stores = await cur.fetchall() or []

            if len(stores) > 1:
                names = ", ".join(s["shop_domain"] for s in stores)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{client['name']} has {len(stores)} installed stores "
                        f"({names}). Automatically pointing them all at one "
                        f"domain would be wrong, so this needs to be sorted out "
                        f"by hand."
                    ),
                )

            previous_client_domain = client.get("domain")
            previous_store_domain = stores[0]["shop_domain"] if stores else None

            # Connections run with autocommit=True, so wrap both writes in an
            # explicit transaction — otherwise a failure on the second statement
            # would leave exactly the half-updated state this endpoint exists to
            # prevent.
            async with conn.transaction():
                await cur.execute(
                    "UPDATE clients SET shopify_domain_name = %s WHERE id = %s;",
                    (domain, client_id),
                )
                if stores:
                    await cur.execute(
                        "UPDATE shopify_stores SET shop_domain = %s, updated_at = now() "
                        " WHERE id = %s;",
                        (domain, stores[0]["id"]),
                    )

    # Resolution caches the domain -> client_id mapping (memory + Redis), so the
    # old and new domains must both be busted or the change takes up to the TTL
    # to be visible.
    for stale in {previous_client_domain, previous_store_domain, domain}:
        if stale:
            try:
                await ainvalidate_shop_domain(stale)
            except Exception as exc:
                logger.warning(f"[ADMIN] cache invalidation failed for {stale}: {exc}")

    if stores:
        message = (
            f"Saved. Both places now say {domain} — the client record and the "
            f"installed Shopify store. Inbound webhooks from this shop will be "
            f"matched to {client['name']}."
        )
    else:
        message = (
            f"Saved. The client record now says {domain}, so inbound webhooks "
            f"will be matched to {client['name']}. The Shopify app is not "
            f"installed for this client yet, so there was no store record to "
            f"update — install it to enable catalog sync."
        )

    logger.info(
        f"[ADMIN] shop domain for {client['name']} ({client_id}) set to {domain} "
        f"(was clients={previous_client_domain!r} stores={previous_store_domain!r})"
    )

    return {
        "status": "ok",
        "message": message,
        "client_id": client_id,
        "client_name": client["name"],
        "shop_domain": domain,
        "updated": {"clients": True, "shopify_stores": bool(stores)},
        "previous": {
            "clients": previous_client_domain,
            "shopify_stores": previous_store_domain,
        },
    }


@router.get("/shopify/shop-domains/ui", include_in_schema=False)
async def shop_domains_ui():
    """Serve the shop-domain admin page.

    Intentionally unauthenticated: the file is an empty shell containing no
    tenant data. It asks for the admin token in the browser and sends it as a
    bearer header on the API calls above, which are authenticated.
    """
    path = os.path.join(
        os.path.dirname(__file__), "..", "static", "admin-shopify-domains.html"
    )
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Admin UI not found")
    return FileResponse(path, media_type="text/html")
