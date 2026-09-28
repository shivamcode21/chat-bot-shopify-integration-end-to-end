"""Client-configured pinned products for bestseller / trending displays.

Lets a store hard-pin a small set of products (configured per client under the
``pinned_bestseller_products`` config key) so they render as carousel cards
alongside the live Upstash ``bestseller = true`` results whenever a customer
asks for trending / best sellers.

Design (AGENTS.md):
- Config-driven & tenant-scoped: the pin list is per-client config, read through
  the three-tier cache (``config_manager.aget_json_config`` → memory/Redis/DB).
- Stateless: pure input→output helpers. The caller (the ``search_products``
  tool) merges the result into its returned product list; the runtime is what
  persists ``recent_products`` — these helpers never touch state.
- Graceful degradation: any misconfig / read error fails open to ``[]`` so
  pinning (a non-critical enhancement) can never break product search.

Expected config shape (``pinned_bestseller_products``)::

    {
      "enabled": true,
      "products": [
        {
          "title": "Signature Hydra Serum",
          "handle": "signature-hydra-serum",
          "url": "https://store.com/products/signature-hydra-serum",
          "image_url": "https://cdn.../serum.jpg",
          "price": 1299
        }
      ]
    }

``handle`` may be omitted if ``url`` contains ``/products/<handle>``.
"""

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_KEY = "pinned_bestseller_products"
# Cap how many pins we surface so a misconfigured list can't flood the carousel.
MAX_PINNED = 3

def is_catalog_wide_bestseller(
    qu_is_catalog_wide: bool,
    *,
    collection_context: Optional[str] = None,
) -> bool:
    """True only for catalog-wide "trending / best sellers" requests.

    Single source of truth is ``qu_is_catalog_wide`` — the Query Understanding
    LLM's own classification (the prompt instructs it to set the flag, defaulting
    to ``False`` when unsure). The LLM understands phrasing/synonyms far better
    than any fixed word list, so the previous token heuristic has been removed.

    The only override is the one signal the LLM can't reliably see: an in-session
    collection-page context. When the customer is browsing a collection,
    "best sellers" means *within that collection*, so pins (which are
    catalog-wide promos) are suppressed regardless of the LLM flag.
    """
    if collection_context:
        return False
    return bool(qu_is_catalog_wide)


def _normalize_pinned(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Coerce one configured pin into a carousel-renderable product dict.

    A card needs at least a ``title`` and a ``handle`` (or a ``/products/<handle>``
    URL to derive it from — mirrors ``format_product_for_carousel``). Returns
    ``None`` when those minimums are missing so a bad entry is skipped rather
    than rendered as a broken card.
    """
    if not isinstance(raw, dict):
        return None

    title = (raw.get("title") or raw.get("name") or "").strip()
    handle = (raw.get("handle") or raw.get("product_handle") or "").strip()
    url = (raw.get("url") or raw.get("product_url") or "").strip()

    if not handle and url:
        m = re.search(r"/products/([^/?#]+)", url)
        if m:
            handle = m.group(1)

    if not title or not handle:
        logger.warning(f"[PINNED] skipping pin missing title/handle: {raw!r}")
        return None

    price = raw.get("price")
    if isinstance(price, (int, float)):
        price = {"min": float(price), "max": float(price)}

    return {
        "title": title,
        "name": title,
        "handle": handle,
        "url": url or None,
        "image_url": raw.get("image_url") or raw.get("image") or "",
        "price": price if isinstance(price, dict) else {},
        "pinned": True,
    }


async def aget_pinned_bestseller_products(client_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return the client's configured pinned bestseller products (≤ ``MAX_PINNED``).

    Empty list when the client has none configured, the feature is disabled,
    or the config is malformed/unreadable.
    """
    if not client_id:
        return []
    try:
        from fashion_bot.config_manager import aget_json_config
        cfg = await aget_json_config(CONFIG_KEY, client_id=client_id)
    except Exception as e:
        logger.warning(f"[PINNED] config read failed for client={client_id}: {e}")
        return []

    if not isinstance(cfg, dict) or cfg.get("enabled") is False:
        return []
    raw_products = cfg.get("products")
    if not isinstance(raw_products, list):
        return []

    out: List[Dict[str, Any]] = []
    for raw in raw_products:
        norm = _normalize_pinned(raw)
        if norm:
            out.append(norm)
            if len(out) >= MAX_PINNED:
                break
    return out


def _handle_of(product: Dict[str, Any]) -> str:
    return str(product.get("handle") or product.get("product_handle") or "").strip().lower()


def merge_pinned_first(
    pinned: List[Dict[str, Any]],
    products: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Prepend ``pinned`` to ``products``, de-duplicating by handle.

    A pin already present in the live results is shown once, in the pinned
    position, so the carousel never renders a duplicate card.
    """
    if not pinned:
        return products
    pinned_handles = {_handle_of(p) for p in pinned if _handle_of(p)}
    tail = [p for p in products if _handle_of(p) not in pinned_handles]
    return list(pinned) + tail
