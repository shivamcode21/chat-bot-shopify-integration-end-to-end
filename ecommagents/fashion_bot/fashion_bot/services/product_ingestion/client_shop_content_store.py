"""
Async CRUD for the client_shop_content_cache table.

One row per client. Holds shop-wide promo / announcement-bar text scraped from
the storefront HTML once per delta-sync run (TTL: 7 days). Webhooks read this
cache and never re-extract.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fashion_bot.database_manager import get_async_postgres_connection

logger = logging.getLogger(__name__)


def _coerce_list(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [t for t in parsed if isinstance(t, str)]
        except Exception:
            return []
    return []


def compute_promo_hash(promo_terms: List[str], shop_image_terms: Optional[List[str]] = None) -> str:
    """Hash both text + image-OCR'd terms so drift in either invalidates the cache."""
    promo = sorted(t.strip() for t in (promo_terms or []) if t and t.strip())
    images = sorted(t.strip() for t in (shop_image_terms or []) if t and t.strip())
    payload = "\n".join(promo) + "\n--\n" + "\n".join(images)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def aget_shop_content(client_id: str) -> Optional[Dict[str, Any]]:
    """Return {promo_terms, shop_image_terms, content_hash, source_url, extracted_at} or None."""
    query = """
        SELECT promo_terms, shop_image_terms, content_hash, source_url, extracted_at
        FROM client_shop_content_cache
        WHERE client_id = %s
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (client_id,))
                row = await cur.fetchone()
                if not row:
                    return None
                d = dict(row) if not isinstance(row, dict) else row
                return {
                    "promo_terms": _coerce_list(d.get("promo_terms")),
                    "shop_image_terms": _coerce_list(d.get("shop_image_terms")),
                    "content_hash": d.get("content_hash") or "",
                    "source_url": d.get("source_url"),
                    "extracted_at": d.get("extracted_at"),
                }
    except Exception as exc:
        logger.warning(f"[SHOP_CONTENT_STORE] read failed for {client_id}: {exc}")
        return None


async def aupsert_shop_content(
    client_id: str,
    promo_terms: List[str],
    shop_image_terms: Optional[List[str]] = None,
    source_url: Optional[str] = None,
) -> bool:
    """Upsert shop content cache row. Returns True on success."""
    shop_image_terms = shop_image_terms or []
    content_hash = compute_promo_hash(promo_terms, shop_image_terms)
    query = """
        INSERT INTO client_shop_content_cache (
            client_id, promo_terms, shop_image_terms, content_hash, source_url, extracted_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (client_id) DO UPDATE
        SET promo_terms = EXCLUDED.promo_terms,
            shop_image_terms = EXCLUDED.shop_image_terms,
            content_hash = EXCLUDED.content_hash,
            source_url = EXCLUDED.source_url,
            extracted_at = EXCLUDED.extracted_at
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    query,
                    (
                        client_id,
                        json.dumps(promo_terms or []),
                        json.dumps(shop_image_terms or []),
                        content_hash,
                        source_url,
                        datetime.now(timezone.utc),
                    ),
                )
        return True
    except Exception as exc:
        logger.error(f"[SHOP_CONTENT_STORE] upsert failed for {client_id}: {exc}")
        return False
