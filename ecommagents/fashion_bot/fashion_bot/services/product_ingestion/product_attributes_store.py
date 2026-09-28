"""
Async CRUD for the product_extracted_attributes table.

Persists LLM-extracted (or manually edited) product attributes in Postgres
so they can be viewed/overridden from the admin UI and synced back to
Upstash Search.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fashion_bot.database_manager import get_async_postgres_connection

logger = logging.getLogger(__name__)


async def aupsert_product_attributes(
    client_id: str,
    product_id: str,
    attrs: Dict[str, Any],
    product_title: Optional[str] = None,
    is_manually_edited: bool = False,
) -> Optional[int]:
    """Upsert extracted attributes for a single product.

    ``attrs`` is a flat dict whose keys match column names
    (category, subcategory, occasion, style, …).  JSONB columns
    (occasion, style, vibe, pairing_tags) are serialised automatically.
    """
    jsonb_cols = {"occasion", "style", "vibe", "pairing_tags"}

    columns = [
        "client_id", "product_id", "product_title", "is_manually_edited",
        "updated_at",
    ]
    values: list = [
        client_id, product_id, product_title, is_manually_edited,
        datetime.now(timezone.utc),
    ]

    allowed = {
        "category", "subcategory", "base_product_name", "product_line",
        "color", "material", "occasion", "style", "vibe", "pairing_tags",
        "segment", "color_family", "pattern", "fit",
    }

    for key, val in attrs.items():
        if key not in allowed:
            continue
        columns.append(key)
        if key in jsonb_cols:
            values.append(json.dumps(val if val is not None else []))
        else:
            values.append(val)

    placeholders = ", ".join(["%s"] * len(values))
    col_str = ", ".join(columns)

    update_parts = []
    for col in columns:
        if col in ("client_id", "product_id"):
            continue
        update_parts.append(f"{col} = EXCLUDED.{col}")
    update_str = ", ".join(update_parts)

    query = f"""
        INSERT INTO product_extracted_attributes ({col_str})
        VALUES ({placeholders})
        ON CONFLICT (client_id, product_id) DO UPDATE
            SET {update_str}
        RETURNING id
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, values)
                row = await cur.fetchone()
                return row["id"] if isinstance(row, dict) else (row[0] if row else None)
    except Exception as e:
        logger.error(f"[ATTR_STORE] upsert failed for {client_id}/{product_id}: {e}")
        return None


async def aupsert_product_attributes_batch(
    client_id: str,
    items: List[Dict[str, Any]],
) -> int:
    """Bulk upsert extracted attributes.

    Each item in *items* must have at least ``product_id`` and may have
    ``product_title``, ``is_manually_edited``, and any attribute column.
    Returns number of rows upserted.
    """
    count = 0
    for item in items:
        pid = item.get("product_id")
        if not pid:
            continue
        result = await aupsert_product_attributes(
            client_id=client_id,
            product_id=pid,
            attrs=item,
            product_title=item.get("product_title"),
            is_manually_edited=item.get("is_manually_edited", False),
        )
        if result is not None:
            count += 1
    return count


async def aget_product_attributes(
    client_id: str,
    product_id: str,
) -> Optional[Dict[str, Any]]:
    """Return attributes for a single product, or None."""
    query = """
        SELECT * FROM product_extracted_attributes
        WHERE client_id = %s AND product_id = %s
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (client_id, product_id))
                row = await cur.fetchone()
                return dict(row) if row else None
    except Exception as e:
        logger.error(f"[ATTR_STORE] get failed for {client_id}/{product_id}: {e}")
        return None


async def alist_product_attributes(
    client_id: str,
    limit: int = 50,
    offset: int = 0,
    manually_edited_only: bool = False,
) -> Dict[str, Any]:
    """Paginated listing of product attributes for a client."""
    where = "WHERE client_id = %s"
    params: list = [client_id]
    if manually_edited_only:
        where += " AND is_manually_edited = TRUE"

    count_query = f"SELECT COUNT(*) FROM product_extracted_attributes {where}"
    data_query = f"""
        SELECT * FROM product_extracted_attributes
        {where}
        ORDER BY updated_at DESC
        LIMIT %s OFFSET %s
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(count_query, params)
                total = (await cur.fetchone())[0]

                await cur.execute(data_query, params + [limit, offset])
                rows = await cur.fetchall()
                items = [dict(r) for r in rows]
                return {"total": total, "items": items, "limit": limit, "offset": offset}
    except Exception as e:
        logger.error(f"[ATTR_STORE] list failed for {client_id}: {e}")
        return {"total": 0, "items": [], "limit": limit, "offset": offset}


async def aget_manually_edited_product_ids(client_id: str) -> Dict[str, Dict[str, Any]]:
    """Return {product_id: attrs_dict} for all manually edited products.

    Used by the ingestion pipeline to skip LLM extraction for products
    where a human has overridden the attributes.
    """
    query = """
        SELECT * FROM product_extracted_attributes
        WHERE client_id = %s AND is_manually_edited = TRUE
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (client_id,))
                rows = await cur.fetchall()
                return {row["product_id"]: dict(row) for row in rows}
    except Exception as e:
        logger.error(f"[ATTR_STORE] get manually edited failed for {client_id}: {e}")
        return {}
