"""
Async CRUD for the product_ocr_summary_cache table.

One row per (client_id, product_id). The row contains the LLM-produced
product OCR summary plus a hash of the image-URL set it was computed from
— so on the next ingestion we can skip the LLM call entirely when the
URL set hasn't changed.

Per-image text dictionary (``per_image_texts``) is stored alongside the
summary so we can reuse it as input context when the URL set changes
and we have to re-run the combined LLM call with cached + new images.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from fashion_bot.database_manager import get_async_postgres_connection

logger = logging.getLogger(__name__)


def compute_url_set_hash(urls: Iterable[str]) -> str:
    """Stable hash of a URL set. Order-independent (we sort); query-string-
    insensitive (we drop ?v= before hashing).
    """
    canonical = sorted({(u.split("?")[0] if u else "") for u in (urls or []) if u})
    return hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()


async def aget_product_ocr_summary(
    client_id: str,
    product_id: str,
) -> Optional[Dict[str, Any]]:
    """Return {url_set_sha256, summary, per_image_texts, model, extracted_at} or None."""
    query = """
        SELECT url_set_sha256, summary, per_image_texts, model, extracted_at
        FROM product_ocr_summary_cache
        WHERE client_id = %s AND product_id = %s
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (client_id, product_id))
                row = await cur.fetchone()
                if not row:
                    return None
                d = dict(row) if not isinstance(row, dict) else row
                per_image = d.get("per_image_texts")
                if isinstance(per_image, str):
                    try:
                        per_image = json.loads(per_image)
                    except Exception:
                        per_image = {}
                return {
                    "url_set_sha256": d.get("url_set_sha256") or "",
                    "summary": d.get("summary") or "",
                    "per_image_texts": per_image or {},
                    "model": d.get("model") or "",
                    "extracted_at": d.get("extracted_at"),
                }
    except Exception as exc:
        logger.warning(
            f"[OCR_SUMMARY_STORE] read failed for {client_id}/{product_id}: {exc}"
        )
        return None


async def aupsert_product_ocr_summary(
    client_id: str,
    product_id: str,
    url_set_sha256: str,
    summary: str,
    per_image_texts: Dict[str, str],
    model: str,
) -> bool:
    """Upsert the row. Overwrites prior summary + URL set hash for this product."""
    query = """
        INSERT INTO product_ocr_summary_cache (
            client_id, product_id, url_set_sha256, summary,
            per_image_texts, model, extracted_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (client_id, product_id) DO UPDATE
        SET url_set_sha256 = EXCLUDED.url_set_sha256,
            summary = EXCLUDED.summary,
            per_image_texts = EXCLUDED.per_image_texts,
            model = EXCLUDED.model,
            extracted_at = EXCLUDED.extracted_at
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    query,
                    (
                        client_id,
                        product_id,
                        url_set_sha256,
                        summary or "",
                        json.dumps(per_image_texts or {}),
                        model or "unknown",
                        datetime.now(timezone.utc),
                    ),
                )
        return True
    except Exception as exc:
        logger.error(
            f"[OCR_SUMMARY_STORE] upsert failed for {client_id}/{product_id}: {exc}"
        )
        return False


async def aget_product_ocr_summaries_bulk(
    client_id: str,
    product_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Bulk read for delta sync's UNCHANGED top-up detection. Returns
    {product_id: row_dict} for products that have a cached summary.
    """
    if not product_ids:
        return {}
    query = """
        SELECT product_id, url_set_sha256, summary, per_image_texts, model, extracted_at
        FROM product_ocr_summary_cache
        WHERE client_id = %s AND product_id = ANY(%s)
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (client_id, product_ids))
                rows = await cur.fetchall()
                out: Dict[str, Dict[str, Any]] = {}
                for row in rows:
                    d = dict(row) if not isinstance(row, dict) else row
                    pid = d.get("product_id")
                    if not pid:
                        continue
                    per_image = d.get("per_image_texts")
                    if isinstance(per_image, str):
                        try:
                            per_image = json.loads(per_image)
                        except Exception:
                            per_image = {}
                    out[pid] = {
                        "url_set_sha256": d.get("url_set_sha256") or "",
                        "summary": d.get("summary") or "",
                        "per_image_texts": per_image or {},
                    }
                return out
    except Exception as exc:
        logger.warning(
            f"[OCR_SUMMARY_STORE] bulk read failed for {client_id}: {exc}"
        )
        return {}
