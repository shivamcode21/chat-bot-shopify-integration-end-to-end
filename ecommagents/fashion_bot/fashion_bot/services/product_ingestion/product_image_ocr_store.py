"""
Async CRUD for the product_image_ocr_cache table.

Stores per-image OCR results keyed by SHA256(image_url) so we never re-run the
vision LLM for an image whose URL we've already processed. Shopify CDN URLs are
content-versioned (?v=...), so a re-uploaded image gets a fresh URL → fresh row.
"""

import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from fashion_bot.database_manager import get_async_postgres_connection

logger = logging.getLogger(__name__)


def url_sha256(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


async def aget_cached_ocr_for_urls(
    client_id: str,
    urls: Iterable[str],
    prompt_version: int,
    model: str,
) -> Dict[str, Dict[str, Any]]:
    """Return {url: cache_row_dict} for URLs already OCR'd at this prompt_version/model.

    Rows from older prompt_versions or different models are ignored so a prompt
    bump or model swap forces re-OCR on next ingestion.
    """
    url_list = [u for u in urls if u]
    if not url_list:
        return {}
    by_sha = {url_sha256(u): u for u in url_list}
    shas = list(by_sha.keys())

    query = """
        SELECT image_url_sha256, image_url, extracted_text, summary_terms,
               has_text, model, prompt_version, extracted_at
        FROM product_image_ocr_cache
        WHERE client_id = %s
          AND prompt_version = %s
          AND model = %s
          AND image_url_sha256 = ANY(%s)
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (client_id, prompt_version, model, shas))
                rows = await cur.fetchall()
                out: Dict[str, Dict[str, Any]] = {}
                for row in rows:
                    row_dict = dict(row) if not isinstance(row, dict) else row
                    sha = row_dict["image_url_sha256"]
                    url = by_sha.get(sha) or row_dict.get("image_url")
                    if url:
                        out[url] = row_dict
                return out
    except Exception as exc:
        logger.warning(f"[OCR_STORE] cache read failed for client {client_id}: {exc}")
        return {}


async def aupsert_ocr_results(
    client_id: str,
    rows: List[Dict[str, Any]],
) -> int:
    """Bulk upsert OCR rows. Each row must have:
    url, source, extracted_text, summary_terms (list), has_text, model, prompt_version.
    """
    if not rows:
        return 0

    query = """
        INSERT INTO product_image_ocr_cache (
            client_id, image_url_sha256, image_url, source,
            extracted_text, summary_terms, has_text, model, prompt_version
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (client_id, image_url_sha256) DO UPDATE
        SET extracted_text = EXCLUDED.extracted_text,
            summary_terms  = EXCLUDED.summary_terms,
            has_text       = EXCLUDED.has_text,
            source         = EXCLUDED.source,
            model          = EXCLUDED.model,
            prompt_version = EXCLUDED.prompt_version,
            extracted_at   = NOW()
    """

    upserted = 0
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                for row in rows:
                    url = row.get("url") or ""
                    if not url:
                        continue
                    await cur.execute(
                        query,
                        (
                            client_id,
                            url_sha256(url),
                            url,
                            row.get("source") or "gallery",
                            row.get("extracted_text") or "",
                            json.dumps(row.get("summary_terms") or []),
                            bool(row.get("has_text")),
                            row.get("model") or "unknown",
                            int(row.get("prompt_version") or 1),
                        ),
                    )
                    upserted += 1
    except Exception as exc:
        logger.error(f"[OCR_STORE] bulk upsert failed for {client_id}: {exc}")
    return upserted


async def asweep_stale_cache(older_than_days: int = 180) -> int:
    """Delete cache rows older than N days. Called by a background job, not on hot path."""
    query = """
        DELETE FROM product_image_ocr_cache
        WHERE extracted_at < NOW() - (%s || ' days')::interval
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (str(older_than_days),))
                return cur.rowcount or 0
    except Exception as exc:
        logger.warning(f"[OCR_STORE] sweep failed: {exc}")
        return 0
