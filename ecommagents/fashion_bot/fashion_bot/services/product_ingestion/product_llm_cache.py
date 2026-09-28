"""
Redis cache for product LLM-extracted fields, content hashes, and stock status.

Eliminates expensive Upstash Search reads in the webhook path by caching
the hash, LLM fields, and stock status in Redis after every upsert.
Stock status is used to decide whether an Upstash Search upsert is needed
at all — if ``in_stock`` and ``available_sizes``/``available_colors`` are
unchanged, the Upstash write is skipped entirely.

Key pattern:
    product_llm:{client_id}:{product_id}

Value (JSON):
    {
        "content_hash_no_inventory": "abc123...",
        "semantic_content_hash": "def456...",     # gates LLM re-extraction
        "llm_fields": { "category": "Bottoms", ... },
        "cron_owned_fields": { "bestseller": true, ... },
        "in_stock": true,
        "available_sizes": ["S", "M", "L"],
        "available_colors": ["Blue", "Red"],
        "upstash_variant_signature": "9f1c..."    # what Upstash actually holds
    }

``upstash_variant_signature`` is a receipt, not a mirror of this cache: it
records the per-variant availability vector that was last **successfully
written to Upstash Search**. Every other field here tracks Shopify, and several
webhook paths refresh Redis while deliberately skipping the Upstash write. Once
those two diverge, a gate that asks "did stock change since Redis?" answers "no"
forever and the index can never self-heal — a restocked variant stays flagged
sold-out in the widget's add-to-cart panel indefinitely. Gating on this receipt
instead asks the only question that matters: *does the index still disagree with
Shopify?*

``cron_owned_fields`` exists because Upstash Search upsert REPLACES a document:
the bestseller flag and engagement analytics are written only by the monthly
cron, so without carrying them across a webhook every product update wiped
them.

TTL: 30 days (refreshed on every upsert).
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

import certifi

logger = logging.getLogger(__name__)

REDIS_URL = (
    os.getenv("REDIS_URL")
    or os.getenv("REDIS_CONNECTION_STRING")
    or "redis://localhost:6379/0"
)

_CACHE_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days
_KEY_PREFIX = "product_llm"

_async_redis_client = None

LLM_EXTRACTED_CONTENT_FIELDS = [
    "base_product_name",
    "product_line",
    "product_line_normalized",
    "category",
    "subcategory",
    "segment",
    "color_family",
    "pattern",
    "material",
    "occasion",
    "style",
    "vibe",
    "pairing_tags",
    # `fit` mirrors NormalizedProduct.fit_type into the search document. It is
    # metafield-sourced but overwritten by _apply_extracted_attributes, so a
    # skipped extraction combined with a throttled enrich blanks it. The delta
    # sync already reads a "fit" key back out of these cached fields
    # (orchestrator._apply stock-update path), which never resolved because
    # nothing wrote it.
    "fit",
]


# Content fields owned by the monthly bestseller/engagement cron, never present
# on a Shopify webhook payload. Upstash Search upsert REPLACES a document, so
# without caching and restoring these, every product webhook demoted a
# bestseller to false and dropped the analytics keys entirely.
CRON_OWNED_CONTENT_FIELDS = [
    "bestseller",
    "unique_sessions",
    "units_sold",
    "conversion_rate",
]


def collect_cron_owned_fields_from_doc(search_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Gather the cron-owned content fields worth carrying across a webhook.

    Falsy values are dropped, which is what makes demotion work: once the cron
    sets ``bestseller`` back to false, there is nothing left to restore and the
    product stays demoted.
    """
    content = search_doc.get("content") or {}
    return {f: content[f] for f in CRON_OWNED_CONTENT_FIELDS if content.get(f)}


async def aupdate_cached_cron_fields(
    client_id: str, documents: List[Dict[str, Any]]
) -> int:
    """Re-sync cached cron-owned fields for documents the cron just upserted.

    The bestseller/engagement cron writes Upstash directly and never touches
    this cache. Without this the Redis copy would keep a stale
    ``bestseller: true`` and the next webhook would restore it — resurrecting a
    product the cron had just demoted. Caching the flag is only safe because
    this exists.

    Only patches entries that already exist; a cache miss means there is
    nothing that could be restored later, so there is nothing to correct.

    Returns the number of cache entries updated.
    """
    client = await _aget_redis_client()
    if not client:
        return 0

    targets = []
    for doc in documents:
        product_id = (doc.get("metadata") or {}).get("product_id", "")
        if product_id:
            targets.append(
                (_cache_key(client_id, product_id), collect_cron_owned_fields_from_doc(doc))
            )
    if not targets:
        return 0

    try:
        raw_entries = await client.mget([key for key, _ in targets])
    except Exception as e:
        logger.warning(f"[PRODUCT_LLM_CACHE] cron-field MGET failed for {client_id}: {e}")
        return 0

    pipe = client.pipeline(transaction=False)
    written = 0
    for (key, cron_fields), raw in zip(targets, raw_entries):
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if cron_fields:
            entry["cron_owned_fields"] = cron_fields
        else:
            entry.pop("cron_owned_fields", None)
        pipe.setex(key, _CACHE_TTL_SECONDS, json.dumps(entry, default=str))
        written += 1

    if written:
        try:
            await pipe.execute()
        except Exception as e:
            logger.warning(f"[PRODUCT_LLM_CACHE] cron-field SET failed for {client_id}: {e}")
            return 0
    return written


def collect_llm_fields_from_doc(search_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Gather every cacheable LLM-derived field from a search document.

    Single source of truth for "which fields did the LLM produce", so the cache
    write and the preserve-on-skip path can never drift apart. Falsy values are
    dropped: caching an empty string would let a skipped extraction overwrite a
    good value with a blank one on the next webhook.

    Only ``content`` is read — ``to_search_document`` puts every LLM-derived
    field there. (``to_vector_data`` additionally exposes ``extracted_color``
    and ``fit_type`` in its metadata, but that is the vector index, which the
    product webhook does not write.)
    """
    content = search_doc.get("content") or {}
    return {f: content[f] for f in LLM_EXTRACTED_CONTENT_FIELDS if content.get(f)}


def _cache_key(client_id: str, product_id: str) -> str:
    return f"{_KEY_PREFIX}:{client_id}:{product_id}"


async def _aget_redis_client():
    global _async_redis_client
    if _async_redis_client is not None:
        return _async_redis_client
    try:
        import redis.asyncio as aioredis
    except Exception:
        return None
    try:
        kwargs: Dict[str, Any] = {"decode_responses": True}
        if str(REDIS_URL).lower().startswith("rediss://"):
            kwargs["ssl_ca_certs"] = certifi.where()
            if os.getenv("REDIS_SSL_INSECURE", "").lower() in ("1", "true", "yes"):
                kwargs["ssl_cert_reqs"] = None
        client = aioredis.Redis.from_url(REDIS_URL, **kwargs)
        await client.ping()
        _async_redis_client = client
        return client
    except Exception as e:
        logger.warning(f"[PRODUCT_LLM_CACHE] Redis init failed: {e}")
        return None


def _build_payload(
    content_hash_no_inventory: str,
    llm_fields: Dict[str, Any],
    in_stock: bool,
    available_sizes: List[str],
    available_colors: List[str],
    variant_snapshot: Optional[List[Dict[str, Any]]] = None,
    semantic_content_hash: Optional[str] = None,
    cron_owned_fields: Optional[Dict[str, Any]] = None,
    upstash_variant_signature: Optional[str] = None,
) -> str:
    data: Dict[str, Any] = {
        "content_hash_no_inventory": content_hash_no_inventory,
        "llm_fields": llm_fields,
        "in_stock": in_stock,
        "available_sizes": sorted(available_sizes),
        "available_colors": sorted(available_colors),
    }
    if variant_snapshot is not None:
        data["variant_snapshot"] = variant_snapshot
    # Carried, never inferred. A caller that did not itself write to Upstash
    # must pass the previous receipt through verbatim; dropping it would claim
    # the index is unknown and buy a redundant patch on the next webhook.
    if upstash_variant_signature:
        data["upstash_variant_signature"] = upstash_variant_signature
    # Optional: entries written before this field existed simply omit it, and
    # readers fall back to the full hash — see llm_inputs_unchanged.
    if semantic_content_hash:
        data["semantic_content_hash"] = semantic_content_hash
    # Omitted when empty so a demoted bestseller leaves nothing to restore.
    if cron_owned_fields:
        data["cron_owned_fields"] = cron_owned_fields
    return json.dumps(data, default=str)


def stock_status_changed(
    cached: Dict[str, Any],
    new_in_stock: bool,
    new_available_sizes: List[str],
    new_available_colors: List[str],
) -> bool:
    """Return *True* if the searchable stock status differs from cached values.

    Product-level only. Do **not** use this to gate an Upstash write — it
    compares against Redis, which several paths refresh without touching the
    index. Use :func:`upstash_variants_stale` for that.
    """
    if cached.get("in_stock") != new_in_stock:
        return True
    if sorted(cached.get("available_sizes") or []) != sorted(new_available_sizes or []):
        return True
    if sorted(cached.get("available_colors") or []) != sorted(new_available_colors or []):
        return True
    return False


UPSTASH_VARIANT_SIGNATURE_KEY = "upstash_variant_signature"


def _variant_availability_pairs(variants: List[Dict[str, Any]]) -> List[Tuple[str, bool]]:
    """Normalize any of our variant shapes to ``[(variant_id, is_available)]``.

    Three shapes reach this function and all three must hash identically for a
    given real-world stock state, or the gate flaps and writes on every webhook:

    - Redis snapshots      -> ``variant_id`` + ``inventoryQuantity``/``inventoryPolicy``
    - Upstash metadata     -> ``id`` + ``available``
    - NormalizedProduct    -> ``id`` + ``available``
    """
    from fashion_bot.services.product_ingestion.shopify_product_service import (
        variant_is_in_stock,
    )

    pairs: List[Tuple[str, bool]] = []
    for v in variants or []:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("variant_id") or v.get("id") or "")
        if "/" in vid:
            vid = vid.split("/")[-1]
        if not vid:
            continue
        if isinstance(v.get("available"), bool):
            available = v["available"]
        else:
            available = variant_is_in_stock(
                v.get("inventoryQuantity", v.get("inventory_quantity", 0)),
                v.get("inventoryPolicy", v.get("inventory_policy", "")),
            )
        pairs.append((vid, bool(available)))
    pairs.sort(key=lambda p: p[0])
    return pairs


def variant_availability_signature(variants: List[Dict[str, Any]]) -> str:
    """Stable fingerprint of *which variants are purchasable*.

    Deliberately blind to quantity: 7-in-stock and 2-in-stock are the same
    signature, so routine stock decrements cost zero Upstash writes. Only a
    variant crossing the in-stock/out-of-stock boundary moves it.
    """
    import hashlib

    joined = ",".join(f"{vid}:{int(avail)}" for vid, avail in _variant_availability_pairs(variants))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def upstash_variants_stale(cached: Optional[Dict[str, Any]], new_signature: str) -> bool:
    """Return *True* when Upstash Search does not hold *new_signature*.

    A cached entry with no receipt (written before this field existed, or by a
    path that could not vouch for the index) is treated as stale, so the first
    webhook after rollout repairs the document. That is a one-time write per
    product, not a recurring cost: the receipt is stamped on the way out.
    """
    if not cached:
        return True
    return cached.get(UPSTASH_VARIANT_SIGNATURE_KEY) != new_signature


def variant_availability_pct(variants: List[Dict[str, Any]]) -> float:
    """Share of variants currently purchasable, as a 0-100 percentage.

    Must stay byte-identical to ``ShopifyProductService._normalize_product`` —
    the search layer filters on this field, so a drift between the two writers
    silently changes which products are retrievable.
    """
    pairs = _variant_availability_pairs(variants)
    if not pairs:
        return 100.0
    return round(sum(1 for _, avail in pairs if avail) / len(pairs) * 100, 1)


# ── GET / SET ────────────────────────────────────────────────────────


async def aget_cached_product_data(
    client_id: str, product_id: str
) -> Optional[Dict[str, Any]]:
    """Return the full cached entry or *None* on miss."""
    client = await _aget_redis_client()
    if not client:
        return None
    try:
        raw = await client.get(_cache_key(client_id, product_id))
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"[PRODUCT_LLM_CACHE] GET failed {client_id}/{product_id}: {e}")
    return None


# Keep old name as alias for backward compat
aget_cached_llm_data = aget_cached_product_data


def llm_inputs_unchanged(cached: Dict[str, Any], new_semantic_hash: str, new_full_hash: str) -> bool:
    """Return *True* when the cached LLM extraction is still valid for this product.

    Compares the semantic hash (product identity — title, product_type,
    description) when the cached entry carries one. Entries written before that
    field existed fall back to the full content hash, i.e. exactly the
    pre-existing behaviour — so rolling this out does not invalidate a single
    cached extraction or trigger a re-ingestion storm. Entries heal to the
    semantic hash the next time they are written.
    """
    cached_semantic = cached.get("semantic_content_hash")
    if cached_semantic:
        return bool(new_semantic_hash) and cached_semantic == new_semantic_hash
    cached_full = cached.get("content_hash_no_inventory")
    return bool(cached_full) and cached_full == new_full_hash


async def aset_cached_product_data(
    client_id: str,
    product_id: str,
    content_hash_no_inventory: str,
    llm_fields: Dict[str, Any],
    in_stock: bool = True,
    available_sizes: List[str] = None,
    available_colors: List[str] = None,
    variant_snapshot: Optional[List[Dict[str, Any]]] = None,
    semantic_content_hash: Optional[str] = None,
    cron_owned_fields: Optional[Dict[str, Any]] = None,
    upstash_variant_signature: Optional[str] = None,
) -> None:
    """Persist hash + LLM fields + stock status (+ optional variant snapshot) in Redis.

    ``upstash_variant_signature`` is the receipt for the index write — pass the
    freshly written signature after a successful Upstash write, or the previous
    entry's value verbatim when this call did not write to Upstash.
    """
    client = await _aget_redis_client()
    if not client:
        return
    payload = _build_payload(
        content_hash_no_inventory,
        llm_fields,
        in_stock,
        available_sizes or [],
        available_colors or [],
        variant_snapshot=variant_snapshot,
        semantic_content_hash=semantic_content_hash,
        cron_owned_fields=cron_owned_fields,
        upstash_variant_signature=upstash_variant_signature,
    )
    try:
        await client.setex(_cache_key(client_id, product_id), _CACHE_TTL_SECONDS, payload)
    except Exception as e:
        logger.warning(f"[PRODUCT_LLM_CACHE] SET failed {client_id}/{product_id}: {e}")


# Keep old name as alias
aset_cached_llm_data = aset_cached_product_data


async def aset_cached_llm_data_bulk(
    client_id: str,
    products: List[Dict[str, Any]],
    failed_doc_ids: Optional[Set[str]] = None,
) -> int:
    """Bulk-cache data for a list of search documents (post-upsert).

    Each entry in *products* must have ``metadata.product_id``,
    ``metadata.content_hash_no_inventory`` and the relevant fields in
    ``content``.

    ``failed_doc_ids`` are the ``doc["id"]`` values whose Upstash upsert failed.
    Those still get cached — the LLM fields and stock status are valid — but
    they get **no index receipt**, because the index never received them.
    Stamping one would assert the index is current and leave a genuinely stale
    document unrepairable, which is the exact failure this receipt exists to
    prevent.

    Returns the number of keys written.
    """
    client = await _aget_redis_client()
    if not client:
        return 0
    written = 0
    try:
        pipe = client.pipeline(transaction=False)
        for doc in products:
            meta = doc.get("metadata") or {}
            content = doc.get("content") or {}
            pid = meta.get("product_id", "")
            h = meta.get("content_hash_no_inventory", "")
            if not pid or not h:
                continue
            llm_fields = collect_llm_fields_from_doc(doc)
            cron_fields = collect_cron_owned_fields_from_doc(doc)
            payload = _build_payload(
                h,
                llm_fields,
                content.get("in_stock", True),
                content.get("available_sizes") or [],
                content.get("available_colors") or [],
                # Present on docs built after the semantic-hash split; absent on
                # older docs, which simply keep full-hash gating.
                semantic_content_hash=meta.get("semantic_content_hash"),
                cron_owned_fields=cron_fields,
                # A doc that upserted was written to Upstash verbatim, so the
                # variants it carries *are* what the index holds — stamping the
                # receipt is what stops the next webhook from re-patching a
                # document that is already correct. A doc that failed gets none,
                # so its next webhook repairs it.
                upstash_variant_signature=(
                    None
                    if failed_doc_ids and doc.get("id") in failed_doc_ids
                    else variant_availability_signature(meta.get("variants") or [])
                ),
            )
            pipe.setex(_cache_key(client_id, pid), _CACHE_TTL_SECONDS, payload)
            written += 1
        if written:
            await pipe.execute()
    except Exception as e:
        logger.warning(f"[PRODUCT_LLM_CACHE] bulk SET failed for {client_id}: {e}")
    return written


# ── Inventory-item → product reverse index ────────────────────────────

_INV_ITEM_PREFIX = "inv_item"
_INV_ITEM_TTL_SECONDS = 90 * 24 * 60 * 60  # same as product cache


def _inv_item_key(client_id: str, inventory_item_id: int | str) -> str:
    return f"{_INV_ITEM_PREFIX}:{client_id}:{inventory_item_id}"


async def aget_product_id_by_inventory_item(
    client_id: str, inventory_item_id: int | str,
) -> Optional[str]:
    """Look up product_id from the reverse index.  Returns *None* on miss."""
    client = await _aget_redis_client()
    if not client:
        return None
    try:
        return await client.get(_inv_item_key(client_id, inventory_item_id))
    except Exception as e:
        logger.warning(
            f"[PRODUCT_LLM_CACHE] inv_item GET failed "
            f"{client_id}/{inventory_item_id}: {e}"
        )
        return None


async def aset_inventory_item_index(
    client_id: str,
    mappings: List[Tuple[int | str, str]],
) -> int:
    """Bulk-set ``inventory_item_id → product_id`` mappings.

    *mappings* is a list of ``(inventory_item_id, product_id)`` tuples.
    Returns the number of keys written.
    """
    client = await _aget_redis_client()
    if not client or not mappings:
        return 0
    try:
        pipe = client.pipeline(transaction=False)
        for inv_id, prod_id in mappings:
            pipe.setex(
                _inv_item_key(client_id, inv_id),
                _INV_ITEM_TTL_SECONDS,
                str(prod_id),
            )
        await pipe.execute()
        return len(mappings)
    except Exception as e:
        logger.warning(
            f"[PRODUCT_LLM_CACHE] inv_item bulk SET failed "
            f"{client_id}: {e}"
        )
        return 0


# ── Inventory upsert dedup ───────────────────────────────────────────

_DEDUP_PREFIX = "inv_dedup"
_DEDUP_TTL_SECONDS = 60


def _dedup_key(client_id: str, product_id: str) -> str:
    return f"{_DEDUP_PREFIX}:{client_id}:{product_id}"


async def atry_claim_inventory_upsert(
    client_id: str, product_id: str
) -> bool:
    """Attempt to claim the right to upsert this product to Upstash Search.

    Uses ``SET NX`` (set-if-not-exists) with a 60-second TTL.  Returns
    *True* if the caller won the claim (should proceed with the upsert),
    *False* if another webhook already handled it.
    """
    client = await _aget_redis_client()
    if not client:
        return True  # fail-open: if Redis is down, allow the upsert
    try:
        result = await client.set(
            _dedup_key(client_id, product_id),
            "1",
            nx=True,
            ex=_DEDUP_TTL_SECONDS,
        )
        return result is not None  # SET NX returns None if key already existed
    except Exception as e:
        logger.warning(f"[PRODUCT_LLM_CACHE] dedup claim failed {client_id}/{product_id}: {e}")
        return True  # fail-open
